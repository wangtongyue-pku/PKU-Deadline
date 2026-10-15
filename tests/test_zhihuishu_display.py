"""Fictional dates only; no copied course names, descriptions or real IDs."""

from datetime import datetime

import pytest

from pku_deadline_agent.sources import SourceRejected
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.zhihuishu_display import parse_display_deadline


@pytest.mark.parametrize("label", ["", "截止时间：", "截止时间: "])
def test_explicit_midnight_is_start_of_displayed_day(label):
    moment, partial = parse_display_deadline(label + "2032.04.09 00:00")
    assert moment == datetime(2032, 4, 9, 0, 0, tzinfo=BEIJING)
    assert moment.isoformat() == "2032-04-09T00:00:00+08:00"
    assert partial is None


def test_explicit_evening_time_uses_beijing_and_never_host_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    moment, partial = parse_display_deadline("2032.12.31 23:59")
    assert moment == datetime(2032, 12, 31, 23, 59, tzinfo=BEIJING)
    assert partial is None


def test_only_calendar_day_does_not_become_midnight():
    assert parse_display_deadline("截止时间：2032.04.09") == (None, "2032-04-09")


@pytest.mark.parametrize("value", [None, "", "  ", "截止时间："])
def test_absent_field_stays_unknown(value):
    assert parse_display_deadline(value) == (None, None)


@pytest.mark.parametrize("value", [
    "2031.02.29 20:00", "2032.13.01 20:00", "2032.04.31 20:00",
    "2032.04.09 24:00", "2032.04.09 12:60", "04.09 20:00",
    "明天20:00", "2032.04.09至2032.04.10", "未公布", "2032.04.09 00:00 0条评论",
    "2032.04.09 20:00\n要求：虚构内容", "2032.04.09 20:00 Cookie=fictional-value",
    123, [], "0" * 81,
], ids=["invalid-leap", "invalid-month", "invalid-day", "24-hour", "invalid-minute",
        "missing-year", "relative", "range", "unverified-marker", "whole-footer",
        "whole-card", "credential-tail", "number", "array", "oversize"])
def test_invalid_or_unscoped_input_is_rejected_without_echo(value):
    with pytest.raises(SourceRejected) as error:
        parse_display_deadline(value)
    assert "fictional-value" not in str(error.value)


def test_valid_leap_day_is_retained():
    assert parse_display_deadline("2032.02.29 08:30")[0] == datetime(2032, 2, 29, 8, 30, tzinfo=BEIJING)
