import sys, pathlib, time, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest


def _ts(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi).timestamp())  # 本地时区


def test_validate_accepts_common_forms():
    from tasks import cron
    for e in ["* * * * *", "0 9 * * *", "*/15 * * * *", "0 9 * * 1-5",
              "30 8,20 1 * *", "0 0 1 1 *"]:
        cron.validate(e)


def test_validate_rejects_bad():
    from tasks import cron
    for e in ["", "* * * *", "* * * * * *", "60 * * * *", "0 24 * * *",
              "0 9 * * 8", "a * * * *", "*/0 * * * *"]:
        with pytest.raises(cron.CronError):
            cron.validate(e)


def test_next_after_daily_9am():
    from tasks import cron
    got = cron.next_after("0 9 * * *", _ts(2026, 8, 16, 8, 30))
    assert got == _ts(2026, 8, 16, 9, 0)


def test_next_after_is_strictly_after():
    from tasks import cron
    exact = _ts(2026, 8, 16, 9, 0)
    assert cron.next_after("0 9 * * *", exact) == _ts(2026, 8, 17, 9, 0)


def test_next_after_step_and_weekday():
    from tasks import cron
    # 2026-08-16 是周日;下一个工作日 9 点是 8/17(周一)
    assert cron.next_after("0 9 * * 1-5", _ts(2026, 8, 16, 10, 0)) == _ts(2026, 8, 17, 9, 0)
    assert cron.next_after("*/15 * * * *", _ts(2026, 8, 16, 9, 7)) == _ts(2026, 8, 16, 9, 15)


def test_sunday_accepts_both_0_and_7():
    from tasks import cron
    a = cron.next_after("0 9 * * 0", _ts(2026, 8, 16, 10, 0))
    b = cron.next_after("0 9 * * 7", _ts(2026, 8, 16, 10, 0))
    assert a == b == _ts(2026, 8, 23, 9, 0)


def test_dom_and_dow_are_or_when_both_restricted():
    """标准 cron 语义:day-of-month 与 day-of-week 都受限时取并集。"""
    from tasks import cron
    # 每月 1 号 或 每周一
    got = cron.next_after("0 9 1 * 1", _ts(2026, 8, 16, 10, 0))
    assert got == _ts(2026, 8, 17, 9, 0)  # 周一先到


def test_no_match_within_horizon_raises():
    from tasks import cron
    with pytest.raises(cron.CronError):
        cron.next_after("0 9 30 2 *", _ts(2026, 1, 1, 0, 0))  # 2月30日不存在
