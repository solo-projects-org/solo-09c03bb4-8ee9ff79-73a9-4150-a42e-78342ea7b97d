"""SQLite persistence for current whole-litre daily archives.

The archive tables intentionally store **only whole litres** per minute.
Uploaded millilitre tables are validated in request memory and never written
to the archive.  A SHA-256 fingerprint of the uploaded table is kept so the
application can recognise a byte-for-byte unchanged full table.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .algorithm import CorrectionPlan, Verification

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS days (
    date TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    minute_count INTEGER NOT NULL,
    first_minute INTEGER NOT NULL,
    last_minute INTEGER NOT NULL,
    total_l INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_minute_count INTEGER NOT NULL,
    verified_ok INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes (
    date TEXT NOT NULL,
    idx INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    litre INTEGER NOT NULL,
    PRIMARY KEY (date, idx),
    FOREIGN KEY (date) REFERENCES days(date) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS previews (
    id TEXT PRIMARY KEY,
    day_date TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    base_fingerprint TEXT NOT NULL,
    p_index INTEGER NOT NULL,
    p_minute INTEGER,
    minutes TEXT NOT NULL,
    new_archive TEXT NOT NULL,
    new_source_sha256 TEXT NOT NULL,
    new_total_l INTEGER NOT NULL,
    check_json TEXT NOT NULL,
    file_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_date TEXT NOT NULL,
    from_index INTEGER NOT NULL,
    from_minute INTEGER,
    old_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,
    changed_minutes INTEGER NOT NULL,
    total_l INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    minutes TEXT NOT NULL,
    archive TEXT NOT NULL,
    file_name TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_day ON revisions(day_date, id);
CREATE INDEX IF NOT EXISTS idx_previews_day ON previews(day_date);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path() -> str:
    return os.environ.get(
        "DATABASE_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "archive.db"),
    )


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may resolve a sync dependency in a worker thread and then run
    # the endpoint in another thread.  A connection is only used by one
    # request at a time, so cross-thread use is safe here; concurrent writes
    # are still serialised by BEGIN IMMEDIATE and the busy timeout.
    conn = sqlite3.connect(
        path, timeout=15, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@dataclass(frozen=True)
class MinuteRow:
    idx: int
    minute: int
    litre: int


@dataclass(frozen=True)
class Day:
    date: str
    version: int
    minute_count: int
    first_minute: int
    last_minute: int
    total_l: int
    source_sha256: str
    source_minute_count: int
    verified_ok: bool
    created_at: str
    updated_at: str
    rows: tuple[MinuteRow, ...]

    @property
    def minutes(self) -> list[int]:
        return [row.minute for row in self.rows]

    @property
    def litres(self) -> list[int]:
        return [row.litre for row in self.rows]


@dataclass(frozen=True)
class Revision:
    id: int
    day_date: str
    from_index: int
    from_minute: int | None
    old_version: int
    new_version: int
    changed_minutes: int
    total_l: int
    source_sha256: str
    minutes: tuple[int, ...]
    archive: tuple[int, ...]
    file_name: str | None
    created_at: str


@dataclass(frozen=True)
class Preview:
    id: str
    day_date: str
    base_version: int
    base_fingerprint: str
    p_index: int
    p_minute: int | None
    minutes: list[int]
    new_archive: list[int]
    new_source_sha256: str
    new_total_l: int
    verification: Verification
    file_name: str | None
    created_at: str


def _day_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Day:
    minute_rows = conn.execute(
        "SELECT idx, minute, litre FROM minutes WHERE date = ? ORDER BY idx",
        (row["date"],),
    ).fetchall()
    return Day(
        date=row["date"],
        version=row["version"],
        minute_count=row["minute_count"],
        first_minute=row["first_minute"],
        last_minute=row["last_minute"],
        total_l=row["total_l"],
        source_sha256=row["source_sha256"],
        source_minute_count=row["source_minute_count"],
        verified_ok=bool(row["verified_ok"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        rows=tuple(
            MinuteRow(idx=r["idx"], minute=r["minute"], litre=r["litre"])
            for r in minute_rows
        ),
    )


def load_day(conn: sqlite3.Connection, date: str) -> Day | None:
    row = conn.execute("SELECT * FROM days WHERE date = ?", (date,)).fetchone()
    if row is None:
        return None
    return _day_from_row(conn, row)


def fingerprint(day: Day) -> str:
    digest = hashlib.sha256()
    digest.update(f"{day.date}|{day.version}|{day.minute_count}\n".encode())
    for row in day.rows:
        digest.update(f"{row.minute}:{row.litre}\n".encode())
    return digest.hexdigest()


def source_fingerprint(minutes: Sequence[int], ml: Sequence[int]) -> str:
    digest = hashlib.sha256()
    for minute, value in zip(minutes, ml):
        digest.update(f"{minute}:{value}\n".encode())
    return digest.hexdigest()


def list_days(conn: sqlite3.Connection) -> list[Day]:
    rows = conn.execute("SELECT * FROM days ORDER BY date DESC").fetchall()
    return [_day_from_row(conn, row) for row in rows]


def create_initial_day(
    conn: sqlite3.Connection,
    date: str,
    minutes: Sequence[int],
    litres: Sequence[int],
    source_sha256: str,
    verified_ok: bool,
) -> Day:
    """Insert a first archive.  Only whole litres are written."""
    if len(minutes) != len(litres):
        raise ValueError("分钟号与整升值数量不一致")
    now = utc_now()
    with transaction(conn):
        existing = conn.execute("SELECT 1 FROM days WHERE date = ?", (date,)).fetchone()
        if existing is not None:
            raise ValueError("该日已有归档")
        conn.execute(
            "INSERT INTO days(date, version, minute_count, first_minute, last_minute,"
            " total_l, source_sha256, source_minute_count, verified_ok, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                date,
                1,
                len(minutes),
                minutes[0],
                minutes[-1],
                sum(litres),
                source_sha256,
                len(minutes),
                1 if verified_ok else 0,
                now,
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO minutes(date, idx, minute, litre) VALUES (?,?,?,?)",
            [
                (date, idx, minute, litre)
                for idx, (minute, litre) in enumerate(zip(minutes, litres))
            ],
        )
    day = load_day(conn, date)
    assert day is not None
    return day


def save_preview(
    conn: sqlite3.Connection,
    preview_id: str,
    day: Day,
    plan: CorrectionPlan,
    minutes: Sequence[int],
    source_sha256: str,
    file_name: str | None,
) -> Preview:
    with transaction(conn):
        # Only one active plan per day; older previews are superseded.
        conn.execute("DELETE FROM previews WHERE day_date = ?", (day.date,))
        conn.execute(
            "INSERT INTO previews(id, day_date, base_version, base_fingerprint,"
            " p_index, p_minute, minutes, new_archive, new_source_sha256,"
            " new_total_l, check_json, file_name, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                preview_id,
                day.date,
                day.version,
                fingerprint(day),
                plan.p_index,
                plan.p_minute,
                json.dumps(list(minutes)),
                json.dumps(list(plan.new_archive)),
                source_sha256,
                sum(plan.new_archive),
                json.dumps(
                    {
                        "ok": plan.verification.ok,
                        "max_interval_error_ml": plan.verification.max_interval_error_ml,
                        "worst_start_index": plan.verification.worst_start_index,
                        "worst_end_index": plan.verification.worst_end_index,
                        "total_ml": plan.verification.total_ml,
                        "total_litres": plan.verification.total_litres,
                        "total_error_ml": plan.verification.total_error_ml,
                        "message": plan.verification.message,
                    },
                    ensure_ascii=False,
                ),
                file_name,
                utc_now(),
            ),
        )
    preview = load_preview(conn, preview_id)
    assert preview is not None
    return preview


def _verification_from_json(raw: str) -> Verification:
    data = json.loads(raw)
    return Verification(
        ok=bool(data["ok"]),
        max_interval_error_ml=int(data["max_interval_error_ml"]),
        worst_start_index=data["worst_start_index"],
        worst_end_index=data["worst_end_index"],
        total_ml=int(data["total_ml"]),
        total_litres=int(data["total_litres"]),
        total_error_ml=int(data["total_error_ml"]),
        message=str(data.get("message", "")),
    )


def load_preview(conn: sqlite3.Connection, preview_id: str) -> Preview | None:
    row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    return Preview(
        id=row["id"],
        day_date=row["day_date"],
        base_version=row["base_version"],
        base_fingerprint=row["base_fingerprint"],
        p_index=row["p_index"],
        p_minute=row["p_minute"],
        minutes=json.loads(row["minutes"]),
        new_archive=json.loads(row["new_archive"]),
        new_source_sha256=row["new_source_sha256"],
        new_total_l=row["new_total_l"],
        verification=_verification_from_json(row["check_json"]),
        file_name=row["file_name"],
        created_at=row["created_at"],
    )


class StalePreview(Exception):
    """Current archive no longer matches the snapshot used to build a plan."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ApplyResult:
    day: Day
    revision: Revision
    old_version: int


def apply_preview(conn: sqlite3.Connection, preview_id: str) -> ApplyResult:
    """Apply a preview atomically, refusing a stale plan.

    The version and fingerprint checks run inside the same immediate write
    transaction as the updates, so a concurrently changed archive cannot be
    overwritten by a plan built from the old state.
    """
    with transaction(conn):
        preview = conn.execute(
            "SELECT * FROM previews WHERE id = ?", (preview_id,)
        ).fetchone()
        if preview is None:
            raise StalePreview("补正方案不存在或已失效，请重新上传核对")
        day = load_day(conn, preview["day_date"])
        if day is None:
            raise StalePreview("该日归档已不存在，请重新核对")
        if day.version != preview["base_version"] or fingerprint(day) != preview[
            "base_fingerprint"
        ]:
            raise StalePreview("现行档在核对后已发生变化")

        new_archive: list[int] = json.loads(preview["new_archive"])
        preview_minutes: list[int] = json.loads(preview["minutes"])
        p_index = int(preview["p_index"])
        if len(new_archive) != len(day.rows) or p_index < 0 or p_index > len(new_archive):
            raise StalePreview("补正方案与现行档不匹配")
        if preview_minutes != day.minutes:
            raise StalePreview("补正方案的分钟批次与现行档不一致")
        if day.litres[:p_index] != new_archive[:p_index]:
            raise StalePreview("补正方案的保留前缀与现行档不一致")
        check = _verification_from_json(preview["check_json"])
        if not check.ok:
            raise StalePreview("补正方案未通过误差核对")

        now = utc_now()
        new_version = day.version + 1
        conn.executemany(
            "UPDATE minutes SET litre = ? WHERE date = ? AND idx = ?",
            [
                (litre, day.date, idx)
                for idx, litre in enumerate(new_archive)
                if idx >= p_index or litre != day.rows[idx].litre
            ],
        )
        conn.execute(
            "UPDATE days SET version = ?, total_l = ?, source_sha256 = ?,"
            " source_minute_count = ?, verified_ok = 1, updated_at = ? WHERE date = ?",
            (
                new_version,
                sum(new_archive),
                preview["new_source_sha256"],
                len(preview_minutes),
                now,
                day.date,
            ),
        )
        new_day = load_day(conn, day.date)
        assert new_day is not None
        if new_day.litres != new_archive:
            raise StalePreview("写入后的归档与方案不一致，已回滚")

        changed_minutes = sum(
            1 for old, new in zip(day.litres, new_archive) if old != new
        )
        cursor = conn.execute(
            "INSERT INTO revisions(day_date, from_index, from_minute, old_version,"
            " new_version, changed_minutes, total_l, source_sha256, minutes, archive,"
            " file_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                day.date,
                p_index,
                preview["p_minute"],
                day.version,
                new_version,
                changed_minutes,
                sum(new_archive),
                preview["new_source_sha256"],
                json.dumps(preview_minutes),
                json.dumps(new_archive),
                preview["file_name"],
                now,
            ),
        )
        revision_id = int(cursor.lastrowid)
        conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))
        revision = Revision(
            id=revision_id,
            day_date=day.date,
            from_index=p_index,
            from_minute=preview["p_minute"],
            old_version=day.version,
            new_version=new_version,
            changed_minutes=changed_minutes,
            total_l=sum(new_archive),
            source_sha256=preview["new_source_sha256"],
            minutes=tuple(preview_minutes),
            archive=tuple(new_archive),
            file_name=preview["file_name"],
            created_at=now,
        )
        return ApplyResult(day=new_day, revision=revision, old_version=day.version)


def _revision_from_row(row: sqlite3.Row) -> Revision:
    return Revision(
        id=row["id"],
        day_date=row["day_date"],
        from_index=row["from_index"],
        from_minute=row["from_minute"],
        old_version=row["old_version"],
        new_version=row["new_version"],
        changed_minutes=row["changed_minutes"],
        total_l=row["total_l"],
        source_sha256=row["source_sha256"],
        minutes=tuple(json.loads(row["minutes"])),
        archive=tuple(json.loads(row["archive"])),
        file_name=row["file_name"],
        created_at=row["created_at"],
    )


def latest_revision(conn: sqlite3.Connection, date: str) -> Revision | None:
    row = conn.execute(
        "SELECT * FROM revisions WHERE day_date = ? ORDER BY id DESC LIMIT 1",
        (date,),
    ).fetchone()
    return None if row is None else _revision_from_row(row)


def list_revisions(conn: sqlite3.Connection, date: str) -> list[Revision]:
    rows = conn.execute(
        "SELECT * FROM revisions WHERE day_date = ? ORDER BY id DESC", (date,)
    ).fetchall()
    return [_revision_from_row(row) for row in rows]


def delete_preview(conn: sqlite3.Connection, preview_id: str) -> bool:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))
        return cursor.rowcount > 0


def delete_day(conn: sqlite3.Connection, date: str) -> bool:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM days WHERE date = ?", (date,))
        conn.execute("DELETE FROM previews WHERE day_date = ?", (date,))
        conn.execute("DELETE FROM revisions WHERE day_date = ?", (date,))
        return cursor.rowcount > 0
