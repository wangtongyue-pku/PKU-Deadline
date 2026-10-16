"""The real-read bridge carries display fields only, never identity or links."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.zhihuishu_reader import ReadTask
from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport
from pku_deadline_agent.zhihuishu_validation import (
    COVERAGE_FIELDS, TASK_FIELDS, decode_report, encode_report, summarize,
)


def real_report(evidence='authorized-real-validation'):
    coverage = ScrollCoverage(5, 5, 2, 2, 0, 5, False, False, True, False,
                              'displayed_total', 3, ('boundary_unverified', 'scroll_limit'))
    tasks = (ReadTask('虚构受限任务甲', '虚构受限课程', datetime(2034, 6, 10, 20, tzinfo=BEIJING),
                      '2034-06-10', 'https://example.invalid/private-link', '11111111',
                      '22222222', '33333333', '未完成'),
             ReadTask('虚构受限任务乙', '虚构受限课程', None, '2034-06-15'))
    return ScrollReport(tasks, coverage, '虚构受限课程', evidence)


def test_round_trip_keeps_only_the_display_fields():
    report = real_report()
    payload = encode_report(report)
    assert set(payload) == {'version', 'evidence', 'course', 'coverage', 'tasks'}
    assert all(set(item) == set(TASK_FIELDS) for item in payload['tasks'])
    assert set(payload['coverage']) == set(COVERAGE_FIELDS)

    # Identity and links never reach the payload, even though the tasks carry them.
    text = json.dumps(payload, ensure_ascii=False)
    for forbidden in ('example.invalid', '11111111', '22222222', '33333333',
                      'source_url', 'observed_task_id', 'observed_course_id',
                      'observed_enrollment_id', 'scan_position', 'identity_verified'):
        assert forbidden not in text

    restored = decode_report(payload)
    assert restored.evidence == report.evidence and restored.course == report.course
    assert restored.coverage == report.coverage
    assert [(t.title, t.course, t.deadline, t.deadline_date, t.status, t.kind)
            for t in restored.tasks] == \
           [(t.title, t.course, t.deadline, t.deadline_date, t.status, t.kind)
            for t in report.tasks]
    assert all(t.source_url is None and t.observed_task_id is None for t in restored.tasks)


@pytest.mark.parametrize('mutate,message', [
    (lambda p: p.update(extra='x'), '未允许的字段'),
    (lambda p: p.update(version=99), '版本不匹配'),
    (lambda p: p.update(evidence='mystery'), '来源标记'),
    (lambda p: p['coverage'].pop('reasons'), '覆盖度字段'),
    (lambda p: p['coverage'].update(issue_counts=[['deadline_missing']]), '异常计数'),
    (lambda p: p['tasks'][0].update(source_url='https://example.invalid/x'), '未允许字段'),
    (lambda p: p['tasks'][0].update(deadline='2034-06-10T20:00:00'), '缺少时区'),
    (lambda p: p['tasks'][0].update(deadline='not-a-date'), None),
    (lambda p: p.update(tasks='nope'), '任务列表无效'),
])
def test_decode_refuses_anything_outside_the_whitelist(mutate, message):
    payload = encode_report(real_report())
    mutate(payload)
    with pytest.raises(ValueError) as error:
        decode_report(payload)
    if message:
        assert message in str(error.value)


def test_issue_counts_survive_the_round_trip():
    """A partial read must be able to say WHICH field codes fired, and how often."""
    report = real_report()
    coverage = replace(report.coverage,
                       issue_counts=(('deadline_missing', 2), ('title_missing', 1)))
    restored = decode_report(encode_report(replace(report, coverage=coverage)))
    assert restored.coverage.issue_counts == (('deadline_missing', 2), ('title_missing', 1))
    assert restored.coverage == coverage


def test_summary_exposes_counts_only():
    summary = summarize(real_report()).as_dict()
    assert set(summary) == {'evidence', 'task_count', 'course_bound', 'titles_present',
                            'deadlines_present', 'statuses_present', 'coverage_complete',
                            'reasons'}
    assert summary['task_count'] == 2 and summary['titles_present'] == 2
    assert summary['deadlines_present'] == 1 and summary['coverage_complete'] is False
    text = json.dumps(summary, ensure_ascii=False)
    assert '虚构受限任务' not in text and '虚构受限课程' not in text and '2034' not in text


def test_shipped_payload_shape_is_json_safe():
    payload = encode_report(real_report())
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
