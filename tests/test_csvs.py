"""CSV parsing and validation tests."""

from __future__ import annotations

import pytest

from app.csvs import (
    CsvError,
    archive_csv,
    parse_upload,
    sample_correction_csv,
    sample_initial_csv,
)


def test_parse_with_header():
    parsed = parse_upload(b"date,minute,ml\n2026-11-20,0,1200\n2026-11-20,1,900\n")
    assert parsed.date == "2026-11-20"
    assert parsed.minutes == [0, 1]
    assert parsed.ml == [1200, 900]


def test_parse_chinese_header_and_date_from_form():
    parsed = parse_upload("分钟号,排水量\n0,100\n1,0\n".encode("utf-8-sig"), "2026-01-02")
    assert parsed.date == "2026-01-02"
    assert parsed.ml == [100, 0]


def test_parse_without_header_two_columns():
    parsed = parse_upload(b"5,10\n6,20\n", "2026-01-02")
    assert parsed.minutes == [5, 6]


def test_parse_without_header_three_columns():
    parsed = parse_upload(b"2026-01-02,5,10\n2026-01-02,6,20\n")
    assert parsed.date == "2026-01-02"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"", "为空"),
        (b"minute,ml\n0,1\n2,3\n", "不连续"),
        (b"minute,ml\n0,1\n0,3\n", "重复"),
        (b"minute,ml\n2,3\n1,3\n", "升序"),
        (b"minute,ml\n0,-1\n", "非负整数"),
        (b"minute,ml\n0,1.5\n", "非负整数"),
        (b"minute,ml\n0,\n", "非负整数"),
        (b"minute,ml\nx,1\n", "分钟号"),
        (b"minute,ml\n0,1\n", "分钟号") if False else (b"minute,ml\n", "数据行"),
        (b"a,b\n1,2\n", "分钟号"),
        (b"minute,ml\n0,1,2\n", "列数"),
        (b"period,ml\n0,1\n", "无法识别"),
    ],
)
def test_parse_rejections(raw, fragment):
    with pytest.raises(CsvError) as exc:
        parse_upload(raw, "2026-01-02")
    assert fragment in str(exc.value)


def test_date_mismatch_between_form_and_csv():
    with pytest.raises(CsvError, match="不一致"):
        parse_upload(b"date,minute,ml\n2026-01-03,0,1\n", "2026-01-02")


def test_multiple_dates_rejected():
    raw = b"date,minute,ml\n2026-01-02,0,1\n2026-01-03,1,1\n"
    with pytest.raises(CsvError, match="日期"):
        parse_upload(raw)


def test_invalid_calendar_date():
    with pytest.raises(CsvError):
        parse_upload(b"date,minute,ml\n2026-02-30,0,1\n")


def test_minute_out_of_range():
    with pytest.raises(CsvError, match="超出"):
        parse_upload(b"minute,ml\n1440,1\n", "2026-01-02")


def test_too_many_minutes():
    raw = b"minute,ml\n" + b"".join(b"%d,1\n" % m for m in range(1441))
    with pytest.raises(CsvError, match="1440"):
        parse_upload(raw, "2026-01-02")


def test_missing_date_required():
    with pytest.raises(CsvError, match="日期"):
        parse_upload(b"minute,ml\n0,1\n")


def test_non_utf8_rejected():
    with pytest.raises(CsvError, match="UTF-8"):
        parse_upload("分钟,ml\n0,1\n".encode("gbk"), "2026-01-02")


def test_samples_are_valid_and_consecutive():
    initial = parse_upload(sample_initial_csv())
    correction = parse_upload(sample_correction_csv())
    assert initial.date == correction.date == "2026-11-20"
    assert len(initial.minutes) == len(correction.minutes) == 1440
    assert initial.minutes == correction.minutes == list(range(1440))
    assert initial.ml != correction.ml


def test_archive_csv_round_trip():
    content = archive_csv("2026-01-02", [0, 1], [2, 3])
    assert content.decode().splitlines() == [
        "date,minute,litre",
        "2026-01-02,0,2",
        "2026-01-02,1,3",
    ]
