"""FastAPI web application for the whole-litre daily archive."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .algorithm import AlgorithmError, build_correction_plan, build_initial_archive
from .csvs import (
    CsvError,
    MAX_UPLOAD_BYTES,
    archive_csv,
    parse_upload,
    sample_correction_csv,
    sample_initial_csv,
)
from .db import source_fingerprint

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DATE = "2026-11-20"


def create_app(db_path: str | None = None) -> FastAPI:
    resolved_db_path = db_path or db.default_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(resolved_db_path)
        yield

    app = FastAPI(title="污水排放整升日档", lifespan=lifespan)
    app.state.db_path = resolved_db_path
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, context: dict, status_code: int = 200):
        base = {
            "sample_date": SAMPLE_DATE,
        }
        base.update(context)
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=base,
            status_code=status_code,
        )

    def download(content: bytes, filename: str) -> Response:
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"; '
                    f"filename*=UTF-8''{quote(filename)}"
                ),
                "Cache-Control": "no-store",
            },
        )

    def day_summary(conn: sqlite3.Connection, day: db.Day) -> dict:
        # The archive stores only whole litres; full interval verification of
        # the millilitre source happens before publish and is recorded via
        # ``verified_ok``.  Revisions are shown for audit.
        return {
            "day": day,
            "revisions": db.list_revisions(conn, day.date),
        }

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_conn)):
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        days = db.list_days(conn)
        summaries = [day_summary(conn, day) for day in days]
        return render(
            request,
            "index.html",
            {
                "summaries": summaries,
                "error": request.query_params.get("error"),
                "notice": request.query_params.get("notice"),
                "sample_exists": any(d.date == SAMPLE_DATE for d in days),
            },
        )

    @app.post("/archive", response_class=HTMLResponse)
    def create_archive(
        request: Request,
        csv_file: UploadFile | None = None,
        date_value: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        raw = b""
        if csv_file is not None:
            raw = csv_file.file.read(MAX_UPLOAD_BYTES + 1)
        if not raw:
            return render(
                request,
                "error.html",
                {"title": "无法归档", "message": "请选择要上传的单日 CSV 文件。"},
                status_code=400,
            )
        try:
            parsed = parse_upload(raw, date_value or None)
        except CsvError as exc:
            return render(
                request,
                "error.html",
                {"title": "CSV 校验失败", "message": str(exc)},
                status_code=400,
            )

        if db.load_day(conn, parsed.date) is not None:
            return render(
                request,
                "error.html",
                {
                    "title": "该日已有归档",
                    "message": (
                        f"{parsed.date} 已有现行档，首次归档不能覆盖。"
                        "如需修改，请在下载/补正区上传同一批分钟的全表进行补正。"
                    ),
                    "day_date": parsed.date,
                },
                status_code=409,
            )

        try:
            litres = build_initial_archive(parsed.ml)
            day = db.create_initial_day(
                conn,
                parsed.date,
                parsed.minutes,
                litres,
                source_fingerprint(parsed.minutes, parsed.ml),
                True,
            )
        except (AlgorithmError, ValueError) as exc:
            return render(
                request,
                "error.html",
                {"title": "无法归档", "message": str(exc)},
                status_code=400,
            )
        return RedirectResponse(
            url=f"/day/{day.date}?notice=archived", status_code=303
        )

    @app.get("/day/{day_date}", response_class=HTMLResponse)
    def day_detail(
        request: Request,
        day_date: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        day = db.load_day(conn, day_date)
        if day is None:
            return render(
                request,
                "error.html",
                {"title": "未找到归档", "message": f"{day_date} 尚无归档，请先上传整日 CSV。"},
                status_code=404,
            )
        summary = day_summary(conn, day)
        latest = summary["revisions"][0] if summary["revisions"] else None
        return render(
            request,
            "day.html",
            {
                **summary,
                "latest": latest,
                "notice": request.query_params.get("notice"),
                "error": request.query_params.get("error"),
                "applied_from": request.query_params.get("from"),
            },
        )

    @app.get("/day/{day_date}/download")
    def download_day(
        day_date: str,
        from_minute: int | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        day = db.load_day(conn, day_date)
        if day is None:
            return Response(
                "该日没有归档", status_code=404, media_type="text/plain; charset=utf-8"
            )

        if from_minute is None:
            content = archive_csv(day.date, [r.minute for r in day.rows], day.litres)
            return download(content, f"archive-{day.date}.csv")

        positions = [i for i, row in enumerate(day.rows) if row.minute == from_minute]
        if not positions:
            return Response(
                "起点分钟不在现行档中", status_code=400, media_type="text/plain; charset=utf-8"
            )
        start = positions[0]
        content = archive_csv(
            day.date,
            [row.minute for row in day.rows[start:]],
            day.litres[start:],
        )
        return download(content, f"archive-{day.date}-from-{from_minute}.csv")

    @app.post("/day/{day_date}/correct", response_class=HTMLResponse)
    def preview_correction(
        request: Request,
        day_date: str,
        csv_file: UploadFile | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        day = db.load_day(conn, day_date)
        if day is None:
            return render(
                request,
                "error.html",
                {"title": "未找到归档", "message": f"{day_date} 尚无归档，不能补正。"},
                status_code=404,
            )
        raw = b""
        filename = None
        if csv_file is not None:
            filename = csv_file.filename
            raw = csv_file.file.read(MAX_UPLOAD_BYTES + 1)
        if not raw:
            return render(
                request,
                "error.html",
                {
                    "title": "无法补正",
                    "message": "请选择同一批分钟的全表 CSV。",
                    "day_date": day.date,
                },
                status_code=400,
            )
        try:
            parsed = parse_upload(raw, day.date)
        except CsvError as exc:
            return render(
                request,
                "error.html",
                {"title": "CSV 校验失败", "message": str(exc), "day_date": day.date},
                status_code=400,
            )

        if parsed.date != day.date:
            return render(
                request,
                "error.html",
                {
                    "title": "日期不一致",
                    "message": f"上传日期 {parsed.date} 与现行档日期 {day.date} 不一致。",
                    "day_date": day.date,
                },
                status_code=400,
            )
        current_minutes = [row.minute for row in day.rows]
        if parsed.minutes != current_minutes:
            return render(
                request,
                "error.html",
                {
                    "title": "分钟批次不一致",
                    "message": (
                        "补正必须上传与现行档同一批分钟的全表。"
                        f"现行档为 {current_minutes[0]}–{current_minutes[-1]} 共 "
                        f"{len(current_minutes)} 分钟。"
                    ),
                    "day_date": day.date,
                },
                status_code=400,
            )

        try:
            plan = build_correction_plan(day.litres, parsed.ml, parsed.minutes)
        except AlgorithmError as exc:
            return render(
                request,
                "error.html",
                {"title": "无法生成补正方案", "message": str(exc), "day_date": day.date},
                status_code=400,
            )

        source_sha = source_fingerprint(parsed.minutes, parsed.ml)
        source_same = source_sha == day.source_sha256
        archive_same = list(plan.new_archive) == day.litres

        preview_id = uuid.uuid4().hex
        preview = db.save_preview(
            conn,
            preview_id,
            day,
            plan,
            parsed.minutes,
            source_sha,
            filename,
        )
        return render(
            request,
            "preview.html",
            {
                "day": day,
                "preview": preview,
                "source_same": source_same,
                "archive_same": archive_same,
                "kept": plan.p_index,
                "total_minutes": len(day.rows),
                "archive_changed_count": sum(
                    1 for old, new in zip(day.litres, plan.new_archive) if old != new
                ),
                "candidate_changes": _candidate_changes(day, plan, limit=12),
                "new_total_l": sum(plan.new_archive),
                "old_total_l": sum(day.litres),
                "new_total_ml": sum(parsed.ml),
            },
        )

    @app.post("/preview/{preview_id}/confirm", response_class=HTMLResponse)
    def confirm_preview(
        request: Request,
        preview_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        preview = db.load_preview(conn, preview_id)
        if preview is None:
            return render(
                request,
                "error.html",
                {
                    "title": "方案已失效，请重算",
                    "message": (
                        "补正方案不存在或已失效（可能对应日档已被替换）。"
                        "旧方案没有发布，请重新上传同一批分钟的全表核对。"
                    ),
                },
                status_code=410,
            )
        try:
            db.apply_preview(conn, preview_id)
        except db.StalePreview as exc:
            return render(
                request,
                "error.html",
                {
                    "title": "现行档已变化，请重算",
                    "message": (
                        f"{exc.reason}。旧方案没有发布，现行档保持原样。"
                        "请重新上传同一批分钟的全表生成新方案。"
                    ),
                    "day_date": preview.day_date,
                },
                status_code=409,
            )
        return RedirectResponse(
            url=(
                f"/day/{preview.day_date}?notice=applied"
                f"&from={preview.p_minute if preview.p_minute is not None else ''}"
            ),
            status_code=303,
        )

    @app.post("/preview/{preview_id}/cancel", response_class=HTMLResponse)
    def cancel_preview(
        request: Request,
        preview_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        preview = db.load_preview(conn, preview_id)
        if preview is None:
            return RedirectResponse(url="/", status_code=303)
        db.delete_preview(conn, preview_id)
        return RedirectResponse(url=f"/day/{preview.day_date}?notice=cancelled", status_code=303)

    @app.get("/samples/initial.csv")
    def sample_initial():
        return download(sample_initial_csv(), "sample-day-2026-11-20.csv")

    @app.get("/samples/correction.csv")
    def sample_correction():
        return download(sample_correction_csv(), "sample-correction-2026-11-20.csv")

    @app.post("/samples/load", response_class=HTMLResponse)
    def load_sample(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        from .csvs import parse_upload as _parse

        parsed = _parse(sample_initial_csv())
        if db.load_day(conn, parsed.date) is not None:
            return RedirectResponse(
                url=f"/day/{parsed.date}?notice=sample_exists", status_code=303
            )
        litres = build_initial_archive(parsed.ml)
        db.create_initial_day(
            conn,
            parsed.date,
            parsed.minutes,
            litres,
            source_fingerprint(parsed.minutes, parsed.ml),
            True,
        )
        return RedirectResponse(url=f"/day/{parsed.date}?notice=sample_loaded", status_code=303)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return render(
            request,
            "error.html",
            {"title": "页面不存在", "message": "请求的页面不存在。"},
            status_code=404,
        )

    return app


def _candidate_changes(day: db.Day, plan, limit: int = 12) -> list[dict]:
    changes = []
    for index, (old_l, new_l) in enumerate(zip(day.litres, plan.new_archive)):
        if old_l != new_l:
            changes.append(
                {
                    "minute": day.rows[index].minute,
                    "new_ml": plan.new_ml[index],
                    "old_l": old_l,
                    "new_l": new_l,
                }
            )
            if len(changes) >= limit:
                break
    return changes


app = create_app()
