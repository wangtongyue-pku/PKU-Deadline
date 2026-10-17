"""Submission filtering is independent of deadlines and local completion.

A passed deadline alone never proves that a student did not submit. Unknown
submission state stays visible for review instead of hiding a possible todo.
"""
import re
import unicodedata

from pku_deadline_agent.time_utils import beijing_now, normalize_datetime


def submission_status(labels):
    """Recognize explicit status text only; conflicting signals remain unknown."""
    if isinstance(labels, str):
        labels = (labels,)
    values = set()
    for text in labels or ():
        if not isinstance(text, str):
            continue
        text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
        if text in ('未完成', '未提交', '尚未提交', '待提交', '逾期未提交', '已逾期未提交'):
            values.add('未完成')
        elif re.fullmatch(r'(?:已完成|已提交|已批改)(?:\((?:待批改|已批改|已评分|逾期|补交)\))?', text):
            values.add('已完成')
    return next(iter(values)) if len(values) == 1 else '未知'


def submission_label(task, now=None):
    if task.status == '已完成':
        return '已提交'
    overdue = (task.deadline is not None and normalize_datetime(task.deadline)
               < normalize_datetime(now if now is not None else beijing_now()))
    if task.status == '未完成':
        return '逾期未提交' if overdue else '未提交'
    return '截止已过 · 提交状态待确认' if overdue else '提交状态待确认'
