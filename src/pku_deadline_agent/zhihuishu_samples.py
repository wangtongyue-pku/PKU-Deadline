"""Explicitly fictional inputs for the development-only review UI. No browser."""

from datetime import datetime

from pku_deadline_agent.models import Task
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.zhihuishu_reader import ReadTask
from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport


def sample_review_data():
    due = datetime(2034, 6, 10, 20, tzinfo=BEIJING)
    course = '虚构翻转课甲'
    tasks = (
        ReadTask('虚构阅读练习', course, due),
        ReadTask('虚构词汇作业', course, due),
        ReadTask('虚构写作作业', course, due.replace(day=12)),
        ReadTask('虚构缺少时刻作业', course, deadline_date='2034-06-15'),
        ReadTask('虚构课程未绑定作业', None, due),
        ReadTask('虚构同名练习', course, due),
        ReadTask('虚构同名练习', course, due.replace(day=11)),
    )
    local = tuple(Task(i, title, course, '作业', due, 'manual', i == 2, due, due)
                  for i, title in enumerate(('虚构词汇作业', '虚构写作作业', '虚构同名练习', '虚构同名练习'), 1))
    coverage = ScrollCoverage(None, 7, 7, 7, 7, 4, True, False, False, False,
                              'unverified', 0, ('boundary_unverified', 'displayed_total_unknown', 'fields_incomplete'))
    return ScrollReport(tasks, coverage, course, 'synthetic-review'), local
