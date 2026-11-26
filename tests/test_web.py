"""End-to-end web flow tests using Starlette's TestClient + SQLite."""

from __future__ import annotations

import csv
import io

import pytest
from starlette.testclient import TestClient

from app import db
from app.algorithm import independent_interval_check
from app.csvs import sample_correction_csv, sample_initial_csv
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def _csv_bytes(rows, header=("date", "minute", "ml")):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode()


def _upload(client, path, raw, field="csv_file"):
    return client.post(
        path,
        files={field: ("data.csv", raw, "text/csv")},
        follow_redirects=False,
    )


def _get_day(client, date="2026-11-20"):
    conn = db.connect(client.app.state.db_path)
    try:
        return db.load_day(conn, date)
    finally:
        conn.close()


def _parse_archive(response):
    reader = csv.DictReader(io.StringIO(response.content.decode()))
    return [(int(r["minute"]), int(r["litre"])) for r in reader]


def test_healthz_and_index(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "首次归档" in page.text


def test_initial_archive_flow_and_equal_interval_invariant(client):
    response = _upload(client, "/archive", sample_initial_csv())
    assert response.status_code == 303
    assert response.headers["location"].startswith("/day/2026-11-20")

    day = _get_day(client)
    assert day is not None
    assert day.version == 1
    assert len(day.rows) == 1440
    assert day.verified_ok
    # The archive itself stores only whole litres.
    conn = db.connect(client.app.state.db_path)
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(minutes)")}
    finally:
        conn.close()
    assert columns == {"date", "idx", "minute", "litre"}
    assert independent_interval_check(_parse_ml(sample_initial_csv()), day.litres) < 1000

    download = client.get("/day/2026-11-20/download")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    rows = _parse_archive(download)
    assert len(rows) == 1440
    assert [l for _, l in rows] == day.litres


def test_duplicate_initial_archive_is_rejected(client):
    _upload(client, "/archive", sample_initial_csv())
    again = _upload(client, "/archive", sample_initial_csv())
    assert again.status_code == 409
    assert "已有现行档" in again.text


def test_invalid_csv_reports_error(client):
    response = _upload(client, "/archive", b"minute,ml\n0,1\n2,3\n")
    assert response.status_code == 400
    assert "不连续" in response.text


def test_correction_preview_and_confirm(client):
    _upload(client, "/archive", sample_initial_csv())
    old = _get_day(client)

    preview = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert preview.status_code == 200
    assert "最晚重发起点" in preview.text
    assert "第 903 分钟" in preview.text
    assert "最大连续时段差" in preview.text

    conn = db.connect(client.app.state.db_path)
    try:
        stored = conn.execute("SELECT * FROM previews").fetchone()
    finally:
        conn.close()
    assert stored is not None
    preview_id = stored["id"]
    p_index = stored["p_index"]
    assert p_index == 903

    confirm = client.post(f"/preview/{preview_id}/confirm", follow_redirects=False)
    assert confirm.status_code == 303
    assert "from=903" in confirm.headers["location"]

    new = _get_day(client)
    assert new.version == 2
    assert new.litres[:p_index] == old.litres[:p_index]
    assert new.litres[p_index:] != old.litres[p_index:]
    assert independent_interval_check(_parse_ml(sample_correction_csv()), new.litres) < 1000
    assert new.source_sha256 == db.source_fingerprint(
        list(range(1440)), _parse_ml(sample_correction_csv())
    )

    suffix = client.get("/day/2026-11-20/download?from_minute=903")
    assert suffix.status_code == 200
    rows = _parse_archive(suffix)
    assert rows[0][0] == 903
    assert len(rows) == 1440 - 903
    assert [l for _, l in rows] == new.litres[903:]

    whole = _parse_archive(client.get("/day/2026-11-20/download"))
    assert whole == list(zip([r.minute for r in new.rows], new.litres))


def _parse_ml(raw):
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    return [int(r["ml"]) for r in rows]


def test_identical_full_table_says_no_resend(client):
    _upload(client, "/archive", sample_initial_csv())
    preview = _upload(client, "/day/2026-11-20/correct", sample_initial_csv())
    assert preview.status_code == 200
    assert "无需重发" in preview.text
    assert "最晚重发起点" not in preview.text
    assert "disabled" in preview.text  # confirm button disabled


def test_metadata_only_change_can_need_no_resend(client):
    raw = _csv_bytes(
        [("2026-11-20", 0, 1000), ("2026-11-20", 1, 1000), ("2026-11-20", 2, 1000)]
    )
    _upload(client, "/archive", raw)
    # Change 1000 -> 1050: the old archive (1,1,1) still has max error 100
    # for every interval, so the whole day can stay unchanged.
    updated = _csv_bytes(
        [("2026-11-20", 0, 1050), ("2026-11-20", 1, 1000), ("2026-11-20", 2, 1000)]
    )
    preview = _upload(client, "/day/2026-11-20/correct", updated)
    assert preview.status_code == 200
    assert "无需重发" in preview.text
    day = _get_day(client)
    assert day.version == 1


def test_stale_preview_is_refused(client):
    _upload(client, "/archive", sample_initial_csv())
    preview = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert preview.status_code == 200

    conn = db.connect(client.app.state.db_path)
    try:
        preview_id = conn.execute("SELECT id FROM previews").fetchone()["id"]
    finally:
        conn.close()

    # Simulate another operator changing the current archive concurrently:
    # bump the version and alter one stored row, leaving the pending plan.
    _mutate_current_archive(client)

    stale = client.post(f"/preview/{preview_id}/confirm")
    assert stale.status_code == 409
    assert "重算" in stale.text
    day = _get_day(client)
    assert day.version == 2  # the other change survived, old plan not applied
    assert day.rows[0].litre == 7  # untouched by stale plan


def _mutate_current_archive(client):
    conn = db.connect(client.app.state.db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE minutes SET litre = 7 WHERE date = ? AND idx = 0",
                ("2026-11-20",),
            )
            conn.execute(
                "UPDATE days SET version = version + 1 WHERE date = ?",
                ("2026-11-20",),
            )
    finally:
        conn.close()


def test_superseded_preview_is_refused(client):
    _upload(client, "/archive", sample_initial_csv())
    first = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert first.status_code == 200
    conn = db.connect(client.app.state.db_path)
    try:
        first_id = conn.execute("SELECT id FROM previews").fetchone()["id"]
    finally:
        conn.close()

    # Building a second plan supersedes the first one.
    second = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert second.status_code == 200

    old = client.post(f"/preview/{first_id}/confirm")
    assert old.status_code in (409, 410)
    assert _get_day(client).version == 1

    conn = db.connect(client.app.state.db_path)
    try:
        second_id = conn.execute("SELECT id FROM previews").fetchone()["id"]
    finally:
        conn.close()
    assert second_id != first_id
    applied = client.post(f"/preview/{second_id}/confirm", follow_redirects=False)
    assert applied.status_code == 303
    assert _get_day(client).version == 2


def _delete_and_rearchive(client):
    conn = db.connect(client.app.state.db_path)
    try:
        db.delete_day(conn, "2026-11-20")
    finally:
        conn.close()
    response = _upload(client, "/archive", sample_initial_csv())
    assert response.status_code == 303


def test_replaced_day_invalidates_pending_plan(client):
    _upload(client, "/archive", sample_initial_csv())
    preview = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert preview.status_code == 200
    conn = db.connect(client.app.state.db_path)
    try:
        preview_id = conn.execute("SELECT id FROM previews").fetchone()["id"]
    finally:
        conn.close()

    _delete_and_rearchive(client)
    stale = client.post(f"/preview/{preview_id}/confirm")
    assert stale.status_code == 410
    assert "重算" in stale.text
    assert _get_day(client).version == 1


def test_wrong_minute_batch_rejected(client):
    _upload(client, "/archive", sample_initial_csv())
    raw = _csv_bytes(
        [("2026-11-20", 0, 1000), ("2026-11-20", 1, 1000)]
    )
    response = _upload(client, "/day/2026-11-20/correct", raw)
    assert response.status_code == 400
    assert "分钟批次不一致" in response.text


def test_correction_without_archive_404(client):
    response = _upload(client, "/day/2030-01-01/correct", sample_initial_csv())
    assert response.status_code == 404


def test_sample_loading_and_downloads(client):
    response = client.post("/samples/load", follow_redirects=False)
    assert response.status_code == 303
    assert _get_day(client) is not None
    again = client.post("/samples/load", follow_redirects=False)
    assert again.status_code == 303
    assert "notice=sample_exists" in again.headers["location"]

    initial = client.get("/samples/initial.csv")
    correction = client.get("/samples/correction.csv")
    assert initial.status_code == correction.status_code == 200
    assert "attachment" in initial.headers["content-disposition"]
    assert len(_parse_ml(initial.content)) == 1440
    assert len(_parse_ml(correction.content)) == 1440


def test_cancel_preview_leaves_archive_unchanged(client):
    _upload(client, "/archive", sample_initial_csv())
    response = _upload(client, "/day/2026-11-20/correct", sample_correction_csv())
    assert response.status_code == 200
    conn = db.connect(client.app.state.db_path)
    try:
        preview_id = conn.execute("SELECT id FROM previews").fetchone()["id"]
    finally:
        conn.close()
    cancel = client.post(f"/preview/{preview_id}/cancel", follow_redirects=False)
    assert cancel.status_code == 303
    day = _get_day(client)
    assert day.version == 1
    conn = db.connect(client.app.state.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 0
    finally:
        conn.close()


def test_day_page_shows_archive_verification(client):
    _upload(client, "/archive", sample_initial_csv())
    page = client.get("/day/2026-11-20")
    assert page.status_code == 200
    assert "归档核对" in page.text
    assert "通过" in page.text
    assert "归档库只保存整升" in page.text


def test_unknown_day_404(client):
    assert client.get("/day/1999-01-01").status_code == 404
    assert client.get("/nope").status_code == 404


def test_download_rejects_minute_outside_archive(client):
    _upload(client, "/archive", sample_initial_csv())
    assert client.get("/day/2026-11-20/download?from_minute=1440").status_code == 400
    assert client.get("/day/2026-11-20/download?from_minute=-1").status_code == 400
    assert client.get("/day/2030-01-01/download").status_code == 404


def test_archive_date_form_must_match_csv(client):
    raw = _csv_bytes([("2026-11-20", 0, 1000), ("2026-11-20", 1, 1000)])
    response = client.post(
        "/archive",
        files={"csv_file": ("d.csv", raw, "text/csv")},
        data={"date_value": "2026-11-21"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "不一致" in response.text
    assert _get_day(client) is None


def test_no_header_csv_requires_form_date(client):
    response = _upload(client, "/archive", b"0,1000\n1,1000\n")
    assert response.status_code == 400
    assert "日期" in response.text


def test_partial_day_range_works(client):
    base = [2000 + (m % 7) for m in range(600, 660)]
    raw = _csv_bytes([("2026-12-01", m, v) for m, v in zip(range(600, 660), base)])
    response = _upload(client, "/archive", raw)
    assert response.status_code == 303
    day = _get_day(client, "2026-12-01")
    assert day.first_minute == 600 and day.last_minute == 659
    assert len(day.rows) == 60
    # Minute 610 becomes a whole litre short: its old "2 L" cannot stay.
    values = list(base)
    values[10] -= 1000
    correct = _csv_bytes(
        [("2026-12-01", m, v) for m, v in zip(range(600, 660), values)]
    )
    preview = _upload(client, "/day/2026-12-01/correct", correct)
    assert preview.status_code == 200
    assert "最晚重发起点：第 610 分钟" in preview.text
    assert "第 600–609 分钟" in preview.text
