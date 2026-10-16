"""The read → preview → match → simulated-import chain, end to end, on fictional pages.

Real-read evidence must preview (clearly marked 部分读取) but never reach any
database: the sandbox import path stays fictional-only until production writes
are explicitly authorized. No AI client and no real site is involved anywhere.
"""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from test_zhihuishu_reader import browser, page  # noqa: F401 - pytest fixtures
from test_zhihuishu_scroll import PROFILE, prepare
from test_zhihuishu_import import setup  # noqa: F401 - pytest fixture (sandbox + repo)

from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.zhihuishu_import import prepare_import, apply_import
from pku_deadline_agent.zhihuishu_reader import ReadTask
from pku_deadline_agent.zhihuishu_review import build_review, is_fictional, origin_label
from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport
from pku_deadline_agent.zhihuishu_session import ReviewReadScope, read_list_for_review

SCRIPT = '''import streamlit as st
from pku_deadline_agent.zhihuishu_review_page import render_zhihuishu_preview_page
target = st.session_state['fixture_target']
report = st.session_state.get('fixture_report')
render_zhihuishu_preview_page(target.path, report=report, import_target=target)
'''


def open_with_report(target, report, *, write=True):
    app = AppTest.from_string(SCRIPT, default_timeout=15)
    app.session_state['fixture_target'] = target
    app.session_state['fixture_report'] = report
    app.run()
    if write and any(r.key == 'zhs_mode' for r in app.radio):
        app.radio(key='zhs_mode').set_value('模拟数据库入库').run()
    assert not app.exception, app.exception
    return app


def partial_report(evidence='authorized-real-validation'):
    """A hand-built report that looks exactly like a bounded real read."""
    coverage = ScrollCoverage(None, 2, 2, 2, 2, 1, True, False, True, False,
                              'unverified', 0, ('boundary_unverified', 'displayed_total_unknown'))
    tasks = (ReadTask('虚构受限真实读取任务甲', '虚构受限课程', datetime_stub(1)),
             ReadTask('虚构受限真实读取任务乙', '虚构受限课程', datetime_stub(2)))
    return ScrollReport(tasks, coverage, '虚构受限课程', evidence)


def datetime_stub(day):
    from datetime import datetime
    from pku_deadline_agent.time_utils import BEIJING
    return datetime(2034, 6, day, 20, 0, tzinfo=BEIJING)


# --------------------------------------------------------------- the full chain


def test_full_chain_from_fictional_page_to_sandbox_import(page, setup):
    target, repo, _ = setup
    before = repo.list_tasks(include_completed=True)
    assert len(before) == 7

    # 1. bounded read of a fictional page. The fixture list lives inside its own
    # scroller (the real page's container is decided by the authorized session);
    # the scope is deliberately not boundary-verified, so the read is partial.
    prepare(page, count=4)
    scope = ReviewReadScope(container='.fixture-scroll', max_scrolls=3)
    report = read_list_for_review(page, PROFILE, scope=scope)
    assert report.evidence == 'synthetic' and is_fictional(report)
    assert report.preview_ready is False  # never authorizes a write by itself
    assert not report.coverage.complete and 'boundary_unverified' in report.coverage.reasons
    assert report.coverage.read_count == 4

    # 2. review against the sandbox's local tasks, read-only. The four fictional
    # titles differ only by a digit, so the shared similarity rule flags them as
    # possible duplicates at PREVIEW time - the user decides, nothing merges.
    snapshot = build_review(report, repo.list_tasks(include_completed=True))
    assert {i.category for i in snapshot.items} == {'可能重复'}
    assert all(i.batch_peers for i in snapshot.items)

    # 3. human selection: import one of the four, leave the rest unchosen. The
    # explicit "still a separate task" acknowledgement is what the page asks for.
    batch = prepare_import(target, snapshot, {1: ImportDecision('new', allow_distinct=True)})
    # 4. confirmed partial import into the marked sandbox
    receipt = apply_import(target, batch, confirmed=True, partial_confirmed=True)

    assert receipt.partial is True          # the list was explicitly partial
    assert len(receipt.task_ids) == 1
    after = repo.list_tasks(include_completed=True)
    assert len(after) == len(before) + 1
    assert {t.source for t in after} - {t.source for t in before} == {'zhihuishu'}
    # The three unchosen reader tasks and every absent local task are untouched.
    assert all(t.title not in {f'虚构滚动任务{i}' for i in (2, 3, 4)} for t in after)
    for old in before:
        assert old in after
    # The write took the documented backup path.
    backups = list(target.path.parent.glob(target.path.name + '.backups/*.sqlite3'))
    assert backups


def test_similar_names_are_flagged_at_preview_and_need_one_acknowledgement(page, setup):
    """Two similar titles must be visible BEFORE the batch is prepared.

    The preview now reuses the write-time matcher, so the user is asked to confirm
    "still separate tasks" while nothing has been written, instead of discovering
    the conflict as a rollback after two confirmations.
    """
    target, repo, _ = setup
    before = repo.list_tasks(include_completed=True)
    prepare(page, count=3)
    scope = ReviewReadScope(container='.fixture-scroll', max_scrolls=3)
    report = read_list_for_review(page, PROFILE, scope=scope)
    snapshot = build_review(report, repo.list_tasks(include_completed=True))

    items = {i.index: i for i in snapshot.items}
    assert items[1].batch_peers == (2, 3) and items[2].batch_peers == (1, 3)
    assert all(i.category == '可能重复' for i in snapshot.items)

    # Without the acknowledgement the plan is refused at PREPARE time - early,
    # before any transaction is opened.
    with pytest.raises(ValueError):
        prepare_import(target, snapshot, {1: ImportDecision('new'), 2: ImportDecision('new')})
    assert repo.list_tasks(include_completed=True) == before
    with sqlite3.connect(target.path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'zhs_extension' not in tables

    # With it, both are written as two separate tasks (the transaction-level guard
    # stays as the second layer, but it is no longer what the user runs into).
    batch = prepare_import(target, snapshot, {
        1: ImportDecision('new', allow_distinct=True),
        2: ImportDecision('new', allow_distinct=True)})
    receipt = apply_import(target, batch, confirmed=True, partial_confirmed=True)
    assert len(receipt.task_ids) == 2
    assert len(set(receipt.task_ids)) == 2
    added = [t for t in repo.list_tasks(include_completed=True) if t.id in receipt.task_ids]
    assert len({t.title for t in added}) == 2


def test_chain_output_matches_what_the_page_shows(page, setup):
    """The preview page renders the reader's own report with the partial marker."""
    target, repo, _ = setup
    prepare(page, count=3)
    scope = ReviewReadScope(container='.fixture-scroll', max_scrolls=3)
    report = read_list_for_review(page, PROFILE, scope=scope)
    app = open_with_report(target, report)
    assert any('部分读取' in w.value for w in app.warning)
    assert any('不能推断' in w.value for w in app.warning)
    assert any(r.key == 'zhs_mode' for r in app.radio)      # fictional → sandbox write offered
    assert any('智慧树（虚构列表）' in str(df.value.get('来源', [])) for df in app.dataframe)
    picks = [c for c in app.checkbox if c.key and c.key.startswith('zhs_review_pick_')]
    assert len(picks) == 3


# ------------------------------------------------- real-read evidence handling


def test_real_read_evidence_previews_but_is_never_writable(setup):
    target, repo, _ = setup
    before = target.path.read_bytes()
    report = partial_report()
    assert origin_label(report) == '受限真实读取（验证模式）'
    assert not is_fictional(report)

    app = open_with_report(target, report, write=False)
    assert any('受限真实读取' in i.value for i in app.info)
    assert any('部分读取' in w.value for w in app.warning)
    assert all(r.key != 'zhs_mode' for r in app.radio)      # no write mode is offered
    assert app.button(key='zhs_review_production_closed').disabled
    assert any('智慧树（受限真实读取）' in str(df.value.get('来源', [])) for df in app.dataframe)
    assert target.path.read_bytes() == before               # nothing written anywhere

    # Even the contract itself refuses real-read evidence, bypassing the UI.
    snapshot = build_review(report, repo.list_tasks(include_completed=True))
    with pytest.raises(PermissionError):
        prepare_import(target, snapshot, {1: ImportDecision('new')})


def test_unknown_evidence_is_refused_by_the_page(setup):
    target, repo, _ = setup
    report = partial_report(evidence='mystery-provenance')
    app = open_with_report(target, report, write=False)
    assert any('缺少可辨认的来源标记' in e.value for e in app.error)
    assert not app.checkbox


def test_validation_mode_fictional_read_is_labeled_and_still_fictional(page):
    prepare(page, count=3)
    report = read_list_for_review(page, PROFILE, validation_only=True)
    assert report.evidence == 'synthetic-validation'
    assert is_fictional(report)
    assert origin_label(report) == '虚构页面读取（验证模式）'


# ------------------------------------------------------------------ scope caps


def test_scope_defaults_cap_the_read_to_five_tasks(page):
    prepare(page, count=23)
    scope = ReviewReadScope(container='.fixture-scroll', max_scrolls=1)
    report = read_list_for_review(page, PROFILE, scope=scope)
    assert report.coverage.observed_count <= 5
    assert not report.coverage.complete
    assert {'scroll_limit', 'task_limit'} & set(report.coverage.reasons)


def test_scope_rejects_out_of_bounds_values():
    for bad in ({'max_tasks': 0}, {'max_tasks': 21}, {'max_scrolls': 0},
                {'max_scrolls': 31}, {'mode': 'unknown'}, {'mode': ''}):
        with pytest.raises(ValueError):
            ReviewReadScope(**bad)
