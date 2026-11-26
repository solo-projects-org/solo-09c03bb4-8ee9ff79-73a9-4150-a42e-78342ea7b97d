"""CSV parsing and export helpers."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Sequence

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_MINUTES_PER_DAY = 1440
MAX_MINUTE_NUMBER = 1439

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_RE = re.compile(r"^\d+$")

DATE_ALIASES = {"date", "日期", "day", "归档日期"}
MINUTE_ALIASES = {"minute", "分钟", "分钟号", "min", "idx"}
ML_ALIASES = {"ml", "毫升", "排水量", "排水量毫升", "volume_ml", "value", "volume"}


class CsvError(ValueError):
    """User-facing CSV validation error."""


@dataclass(frozen=True)
class ParsedCsv:
    date: str
    minutes: list[int]
    ml: list[int]


def parse_date(raw: str) -> str:
    value = (raw or "").strip()
    if not _DATE_RE.match(value):
        raise CsvError("日期格式应为 YYYY-MM-DD")
    try:
        parsed = date_cls.fromisoformat(value)
    except ValueError as exc:
        raise CsvError("日期不是有效的日历日期") from exc
    if parsed.isoformat() != value:
        raise CsvError("日期格式应为 YYYY-MM-DD")
    return value


def _normalise_header(cell: str) -> str:
    return cell.strip().lower().replace("﻿", "")


def parse_upload(raw: bytes, default_date: str | None = None) -> ParsedCsv:
    """Parse a single-day CSV.

    Accepted layouts (header is optional):

    * ``minute,ml``
    * ``date,minute,ml``
    * Chinese aliases such as ``分钟号,排水量`` / ``日期,分钟,毫升``

    Minute numbers must be consecutive in ascending order.  Values must be
    non-negative integers in millilitres.
    """
    if not raw:
        raise CsvError("上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise CsvError("文件超过 2 MB 限制")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("文件必须为 UTF-8 编码的 CSV") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvError("CSV 没有数据行")

    first = [_normalise_header(cell) for cell in rows[0]]
    has_header = any(cell in DATE_ALIASES | MINUTE_ALIASES | ML_ALIASES for cell in first)
    if has_header:
        columns = _map_header(first)
        data_rows = rows[1:]
    else:
        columns = _infer_columns(first)
        data_rows = rows

    if not data_rows:
        raise CsvError("CSV 没有数据行")

    seen_date: str | None = None
    minutes: list[int] = []
    values: list[int] = []
    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        if len(row) != len(columns):
            raise CsvError(f"第 {line_no} 行列数为 {len(row)}，应为 {len(columns)}")
        record = {name: row[pos].strip() for name, pos in columns.items()}

        if "date" in record:
            row_date = parse_date(record["date"])
            if seen_date is None:
                seen_date = row_date
            elif row_date != seen_date:
                raise CsvError(f"第 {line_no} 行日期与前面的行不一致")

        minute_raw = record["minute"]
        if not _INT_RE.match(minute_raw):
            raise CsvError(f"第 {line_no} 行分钟号必须为非负整数")
        minute = int(minute_raw)
        if minute > MAX_MINUTE_NUMBER:
            raise CsvError(f"第 {line_no} 行分钟号 {minute} 超出单日范围")
        if minutes and minute != minutes[-1] + 1:
            if minute == minutes[-1]:
                raise CsvError(f"第 {line_no} 行分钟号重复")
            if minute < minutes[-1]:
                raise CsvError(f"第 {line_no} 行分钟号未按升序排列")
            raise CsvError(
                f"第 {line_no} 行分钟号 {minute} 与上一分钟 {minutes[-1]} 不连续"
            )

        value_raw = record["ml"]
        if not _INT_RE.match(value_raw):
            raise CsvError(f"第 {line_no} 行排水量必须为非负整数毫升")
        value = int(value_raw)
        if value > 10**12:
            raise CsvError(f"第 {line_no} 行排水量过大")
        minutes.append(minute)
        values.append(value)

    if len(minutes) > MAX_MINUTES_PER_DAY:
        raise CsvError("单日最多 1440 个分钟点")
    if not minutes:
        raise CsvError("CSV 没有有效数据行")

    if seen_date is not None:
        csv_date = seen_date
        if default_date:
            form_date = parse_date(default_date)
            if form_date != csv_date:
                raise CsvError(
                    f"表单日期 {form_date} 与 CSV 内日期 {csv_date} 不一致"
                )
    elif default_date:
        csv_date = parse_date(default_date)
    else:
        raise CsvError("CSV 没有日期列，请在表单中填写归档日期")

    return ParsedCsv(date=csv_date, minutes=minutes, ml=values)


def _map_header(header: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for pos, cell in enumerate(header):
        if cell in DATE_ALIASES:
            key = "date"
        elif cell in MINUTE_ALIASES:
            key = "minute"
        elif cell in ML_ALIASES:
            key = "ml"
        else:
            raise CsvError(f"无法识别的列名“{cell}”")
        if key in columns:
            raise CsvError(f"列“{cell}”重复")
        columns[key] = pos
    missing = {"minute", "ml"} - set(columns)
    if missing:
        labels = {"minute": "分钟号", "ml": "毫升"}
        raise CsvError("缺少列：" + "、".join(labels[m] for m in sorted(missing)))
    return columns


def _infer_columns(first: list[str]) -> dict[str, int]:
    if len(first) == 2:
        return {"minute": 0, "ml": 1}
    if len(first) == 3:
        return {"date": 0, "minute": 1, "ml": 2}
    raise CsvError("无表头 CSV 应为两列（分钟号,毫升）或三列（日期,分钟号,毫升）")


def archive_csv(date: str, minutes: Sequence[int], litres: Sequence[int]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "minute", "litre"])
    for minute, litre in zip(minutes, litres):
        writer.writerow([date, minute, litre])
    return buffer.getvalue().encode("utf-8")


def sample_initial_csv() -> bytes:
    """Deterministic 1440-minute sample with realistic fractional litres."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "minute", "ml"])
    for minute in range(1440):
        base = 820 + (minute * 37) % 260
        # Slow daily pattern, in whole millilitres.
        wave = 180 if 6 * 60 <= minute < 22 * 60 else -120
        writer.writerow(["2026-11-20", minute, base + wave])
    return buffer.getvalue().encode("utf-8")


def sample_correction_csv() -> bytes:
    """Same batch as the initial sample, with late-minute meter corrections."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "minute", "ml"])
    for minute in range(1440):
        base = 820 + (minute * 37) % 260
        wave = 180 if 6 * 60 <= minute < 22 * 60 else -120
        value = base + wave
        if minute >= 900:
            # Meter drift correction: upwards, including two 5 L jumps.
            value += 7 + (minute % 5)
            if minute in (1100, 1101, 1102):
                value += 5000
        writer.writerow(["2026-11-20", minute, value])
    return buffer.getvalue().encode("utf-8")
