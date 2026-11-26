"""Regression tests for pending-preview preservation and save-time guards.

These cover the concurrency fixes on the correction page:

* uploading a table identical to the current source must not replace a
  pending plan;
* a plan whose base archive changed while it was being computed must not be
  saved over a newer pending plan (HTTP 409 + "重算");
* metadata-only source changes (same whole-litre archive) remain confirmable
  and update the stored source fingerprint;
* parallel valid saves never produce a 500; last committed save wins;
* slow validation/planning holds no database lock, so confirmation proceeds.
"""

from __future__ import annotations

import csv
import io
import threading

import pytest
from starlette.testclient import TestClient

from app import db
from app.csvs import sample_correction_csv, sample_initial_csv
from app.main import create_app
import app.main as main_mod


DAY = "2026-11-20"
CORRECT_URL = f"/day/{DAY}/correct"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "preview-guard.db"))
    with TestClient(app) as test_client:
        yield test_client


def _upload(client, path, raw, field="csv_file"):
    return client.post(
        path,
        files={field: ("data.csv", raw, "text/csv")},
        follow_redirects=False,
    )


def _preview_ids(client):
    conn = db.connect(client.app.state.db_path)
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM previews")]
    finally:
        conn.close()


def _only_preview_id(client):
    ids = _preview_ids(client)
    assert len(ids) == 1, ids
    return ids[0]


def _day(client):
    conn = db.connect(client.app.state.db_path)
    try:
        return db.load_day(conn, DAY)
    finally:
        conn.close()


def _tweak(raw: bytes, minute: int, delta: int) -> bytes:
    rows = list(csv.reader(io.StringIO(raw.decode())))
    for row in rows[1:]:
        if int(row[1]) == minute:
            row[2] = str(int(row[2]) + delta)
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode()


def test_identical_source_upload_keeps_existing_pending_plan(client):
    _upload(client, "/archive", sample_initial_csv())
    page = _upload(client, CORRECT_URL, sample_correction_csv())
    assert page.status_code == 200
    plan_id = _only_preview_id(client)

    same = _upload(client, CORRECT_URL, sample_initial_csv())
    assert same.status_code == 200
    assert "无需重发" in same.text
    # The existing pending plan is untouched and offered on the page.
    assert _preview_ids(client) == [plan_id]
    assert f"/preview/{plan_id}/confirm" in same.text

    confirmed = client.post(
        f"/preview/{plan_id}/confirm", follow_redirects=False
    )
    assert confirmed.status_code == 303
    assert _day(client).version == 2


def test_identical_source_without_plan_persists_nothing_but_disables(client):
    _upload(client, "/archive", sample_initial_csv())
    page = _upload(client, CORRECT_URL, sample_initial_csv())
    assert page.status_code == 200
    assert "无需重发" in page.text
    assert "disabled" in page.text
    assert _preview_ids(client) == []


def test_day_page_lists_pending_plan(client):
    _upload(client, "/archive", sample_initial_csv())
    _upload(client, CORRECT_URL, sample_correction_csv())
    plan_id = _only_preview_id(client)
    page = client.get(f"/day/{DAY}")
    assert page.status_code == 200
    assert "待确认补正方案" in page.text
    assert f"/preview/{plan_id}/confirm" in page.text


def test_stale_save_prompts_recalc_and_keeps_newer_plan(client, monkeypatch):
    _upload(client, "/archive", sample_initial_csv())
    corr_a = sample_correction_csv()
    corr_b = _tweak(corr_a, 1200, 11)
    corr_c = _tweak(corr_a, 1300, 17)

    _upload(client, CORRECT_URL, corr_a)
    old_id = _only_preview_id(client)
    applied = client.post(f"/preview/{old_id}/confirm", follow_redirects=False)
    assert applied.status_code == 303

    _upload(client, CORRECT_URL, corr_b)
    new_id = _only_preview_id(client)
    assert new_id != old_id

    real_build = main_mod.build_correction_plan

    def build_then_archive_moves(old_archive, new_ml, minutes):
        plan = real_build(old_archive, new_ml, minutes)
        conn = db.connect(client.app.state.db_path)
        try:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE minutes SET litre = 7 WHERE date = ? AND idx = 0",
                    (DAY,),
                )
                conn.execute(
                    "UPDATE days SET version = version + 1 WHERE date = ?",
                    (DAY,),
                )
        finally:
            conn.close()
        return plan

    monkeypatch.setattr(main_mod, "build_correction_plan", build_then_archive_moves)
    stale = _upload(client, CORRECT_URL, corr_c)
    assert stale.status_code == 409
    assert "重算" in stale.text
    # The plan built on the newer state is preserved, not clobbered.
    assert _preview_ids(client) == [new_id]

    # The old page's id is refused with a recalc prompt, never a 500.
    monkeypatch.undo()
    old_confirm = client.post(f"/preview/{old_id}/confirm")
    assert old_confirm.status_code in (409, 410)
    assert "重算" in old_confirm.text


def test_save_preview_db_guard_raises_and_preserves_existing_row(client):
    from app.algorithm import build_correction_plan

    _upload(client, "/archive", sample_initial_csv())
    raw = sample_correction_csv()
    parsed = main_mod.parse_upload(raw, DAY)

    conn = db.connect(client.app.state.db_path)
    try:
        snapshot = db.load_day(conn, DAY)
        plan = build_correction_plan(snapshot.litres, parsed.ml, parsed.minutes)
        keeper = db.save_preview(
            conn, "keeper-id", snapshot, plan, parsed.minutes, "sha-keeper", "k.csv"
        )
        assert keeper.id == "keeper-id"

        # Another committer changes the archive after the stale snapshot.
        with db.transaction(conn):
            conn.execute(
                "UPDATE days SET version = version + 1 WHERE date = ?", (DAY,)
            )

        with pytest.raises(db.StalePreview):
            db.save_preview(
                conn,
                "stale-id",
                snapshot,
                plan,
                parsed.minutes,
                "sha-stale",
                "s.csv",
            )
        rows = [r[0] for r in conn.execute("SELECT id FROM previews")]
        assert rows == ["keeper-id"]
    finally:
        conn.close()


def test_metadata_only_change_is_confirmable_and_updates_source(client):
    def tiny_csv(first_ml):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(("date", "minute", "ml"))
        for minute, value in enumerate((first_ml, 1000, 1000)):
            writer.writerow(("2026-12-01", minute, value))
        return buffer.getvalue().encode()

    _upload(client, "/archive", tiny_csv(1000))
    before = _day_for(client, "2026-12-01")
    assert before.litres == [1, 1, 1]

    response = _upload(
        client, "/day/2026-12-01/correct", tiny_csv(1050)
    )
    assert response.status_code == 200
    assert "无需重发" in response.text
    assert "确认仅更新原表记录" in response.text
    plan_id = _only_preview_id_for(client, "2026-12-01")

    confirmed = client.post(
        f"/preview/{plan_id}/confirm", follow_redirects=False
    )
    assert confirmed.status_code == 303
    after = _day_for(client, "2026-12-01")
    assert after.version == 2
    assert after.litres == [1, 1, 1]
    assert after.source_sha256 != before.source_sha256
    assert after.source_sha256 == db.source_fingerprint(
        [0, 1, 2], [1050, 1000, 1000]
    )


def _day_for(client, date):
    conn = db.connect(client.app.state.db_path)
    try:
        return db.load_day(conn, date)
    finally:
        conn.close()


def _only_preview_id_for(client, date):
    conn = db.connect(client.app.state.db_path)
    try:
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM previews WHERE day_date = ?", (date,)
            )
        ]
    finally:
        conn.close()
    assert len(rows) == 1, rows
    return rows[0]


def test_parallel_valid_corrections_never_500_and_last_save_wins(client):
    _upload(client, "/archive", sample_initial_csv())
    statuses: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            response = _upload(client, CORRECT_URL, sample_correction_csv())
            with lock:
                statuses.append(response.status_code)
        except Exception as exc:  # pragma: no cover - failure signal
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors, errors
    assert statuses and all(code < 500 for code in statuses), statuses
    winner = _only_preview_id(client)
    confirmed = client.post(f"/preview/{winner}/confirm", follow_redirects=False)
    assert confirmed.status_code == 303
    assert _day(client).version == 2


def test_confirmation_is_not_blocked_by_slow_planning(client, monkeypatch):
    _upload(client, "/archive", sample_initial_csv())
    _upload(client, CORRECT_URL, sample_correction_csv())
    plan_id = _only_preview_id(client)

    entered = threading.Event()
    release_planning = threading.Event()
    confirm_done = threading.Event()
    confirm_status: list[int] = []
    real_build = main_mod.build_correction_plan

    def slow_build(old_archive, new_ml, minutes):
        entered.set()
        assert release_planning.wait(10)
        return real_build(old_archive, new_ml, minutes)

    monkeypatch.setattr(main_mod, "build_correction_plan", slow_build)

    def slow_upload():
        _upload(client, CORRECT_URL, sample_correction_csv())

    def confirm_worker():
        response = client.post(
            f"/preview/{plan_id}/confirm", follow_redirects=False
        )
        confirm_status.append(response.status_code)
        confirm_done.set()

    upload_thread = threading.Thread(target=slow_upload)
    upload_thread.start()
    try:
        assert entered.wait(5)
        confirm_thread = threading.Thread(target=confirm_worker)
        confirm_thread.start()
        # Confirmation must finish while planning is still blocked: no lock
        # or global critical section may span validation/planning.
        assert confirm_done.wait(5), "confirmation blocked by planning"
        assert confirm_status == [303]
        confirm_thread.join(5)
    finally:
        release_planning.set()
        upload_thread.join(10)
