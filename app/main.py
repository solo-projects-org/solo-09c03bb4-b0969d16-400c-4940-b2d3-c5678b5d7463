"""FastAPI 网页应用。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import csv_import, matching, readers, service
from .db import init_db

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "app.db"))


def _db() -> str:
    return os.environ.get("DB_PATH", DB_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(_db())
    yield


app = FastAPI(title="景区接驳车等待核查", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _flash_redirect(path: str, message: str, level: str = "ok") -> RedirectResponse:
    query = urlencode({"message": message, "level": level})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def _render_index(
    request: Request,
    message: str | None,
    level: str,
    status_code: int = 200,
):
    sessions = service.list_sessions(_db())
    items = [
        {
            "session": session,
            "result": result,
            "explanation": service.describe_status(session, result),
        }
        for session, result in sessions
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "items": items,
            "calibration": service.current_calibration(_db()),
            "verified_sessions": [s for s, _ in sessions if s.verified_at],
            "message": message,
            "level": level,
            "threshold": matching.DEFAULT_THRESHOLD,
            "limit": matching.DEFAULT_LIMIT,
        },
        status_code=status_code,
    )


def _session_payload(session_id: int) -> dict:
    detail = service.get_detail(_db(), session_id)
    session = detail["session"]
    result = detail["result"]
    credentials = detail["credentials"]
    return {
        "session": {
            "id": session.id,
            "filename": session.filename,
            "minute_base": session.minute_base,
            "last_minute": session.last_minute,
            "duration": session.duration,
            "total_passengers": session.total_passengers,
            "threshold": session.threshold,
            "limit": session.limit_count,
            "source": session.source,
            "source_label": session.source_label,
            "verified_at": session.verified_at,
            "created_at": session.created_at,
        },
        "result": {
            "lower": result.lower,
            "upper": result.upper,
            "status": result.status,
            "status_label": result.status_label,
            "pinned_overdue": result.pinned_overdue,
            "residual_lower": result.residual_lower,
            "residual_upper": result.residual_upper,
            "version": result.version,
            "updated_at": result.updated_at,
        },
        "credentials": [
            {
                "credential_no": c.credential_no,
                "join_minute": c.join_minute,
                "board_minute": c.board_minute,
                "count": c.count,
                "wait_minutes": c.wait_minutes,
                "overdue": c.is_overdue(session.threshold),
            }
            for c in credentials
        ],
        "credential_passengers": detail["credential_passengers"],
        "overdue_credentials": detail["overdue_credentials"],
        "minutes": detail["minutes"],
        "explanation": service.describe_status(session, result),
    }


# ---------------------------------------------------------------- HTML 页面


@app.get("/", response_class=HTMLResponse)
def index(request: Request, message: str | None = None, level: str = "ok"):
    return _render_index(request, message, level)


@app.post("/sessions")
def upload_session(request: Request, file: UploadFile = File(...)):
    filename = file.filename or "upload.csv"
    data = file.file.read()
    try:
        parsed = csv_import.parse_csv(data, filename)
        session_id = service.create_session(_db(), parsed)
    except (csv_import.CsvError, matching.InfeasibleError, ValueError) as exc:
        return _render_index(request, f"导入失败，整表退回：{exc}", "error", status_code=400)
    notice = "CSV 已导入，结论已生成。"
    if parsed.filled_minutes:
        preview = "、".join(str(m) for m in parsed.filled_minutes[:5])
        suffix = " 等" if len(parsed.filled_minutes) > 5 else ""
        notice += f"（表中未列出的第 {preview}{suffix} 分钟按 0 人计）"
    return _flash_redirect(f"/sessions/{session_id}", notice)


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(
    request: Request,
    session_id: int,
    message: str | None = None,
    level: str = "ok",
):
    try:
        detail = service.get_detail(_db(), session_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            **detail,
            "explanation": service.describe_status(detail["session"], detail["result"]),
            "message": message,
            "level": level,
        },
    )


@app.post("/sessions/{session_id}/credentials")
def add_credential_form(
    session_id: int,
    credential_no: str = Form(...),
    join_minute: int = Form(...),
    board_minute: int = Form(...),
    count: int = Form(1),
):
    try:
        outcome = service.add_credential(
            _db(), session_id, credential_no, join_minute, board_minute, count
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CredentialConflictError as exc:
        return _flash_redirect(f"/sessions/{session_id}", str(exc), "error")
    except service.CredentialInfeasibleError as exc:
        return _flash_redirect(
            f"/sessions/{session_id}", f"未保存新凭证：{exc}", "error"
        )
    if outcome.outcome == "duplicate":
        return _flash_redirect(
            f"/sessions/{session_id}",
            f"凭证 {outcome.credential.credential_no} 内容相同，只计一次，结论未变。",
        )
    return _flash_redirect(
        f"/sessions/{session_id}",
        f"凭证 {outcome.credential.credential_no} 已采信，结论已重算。",
    )


@app.post("/sessions/{session_id}/verify")
def verify_session_form(session_id: int):
    try:
        session = service.mark_session_verified(_db(), session_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _flash_redirect(
        f"/sessions/{session_id}",
        f"场次 #{session_id} 已标记为人工核对（{session.verified_at}），可用作读头对照。",
    )


@app.post("/calibrations")
def create_calibration_form(
    request: Request,
    session_id: int = Form(...),
    file: UploadFile = File(...),
):
    filename = file.filename or "readers.csv"
    data = file.file.read()
    try:
        table = readers.parse_reader_csv(data, filename)
        calibration_id = service.create_calibration(_db(), session_id, table)
    except csv_import.CsvError as exc:
        return _render_index(request, f"对照读头表退回：{exc}", "error", status_code=400)
    except service.NotFoundError as exc:
        return _render_index(request, str(exc), "error", status_code=404)
    except service.CalibrationError as exc:
        return _render_index(request, f"对照未通过：{exc}", "error", status_code=400)
    calibration = service.current_calibration(_db())
    count = len(calibration.assignments) if calibration else 0
    return _flash_redirect(
        "/",
        f"对照 #{calibration_id} 已建立并生效：读头 {len(table.readers)} 个，"
        f"可行分法 {count} 种。",
    )


@app.post("/imports/preview")
def preview_import_form(request: Request, file: UploadFile = File(...)):
    filename = file.filename or "readers.csv"
    data = file.file.read()
    try:
        table = readers.parse_reader_csv(data, filename)
        preview_id = service.create_preview(_db(), table)
    except csv_import.CsvError as exc:
        return _render_index(request, f"读头表退回：{exc}", "error", status_code=400)
    except service.PreviewError as exc:
        return _render_index(request, f"读头表退回：{exc}", "error", status_code=400)
    notice = "预览已生成：各可行分法换算出的分钟表相同且整场能配齐，确认后建场。"
    if table.filled_minutes:
        preview = "、".join(str(m) for m in table.filled_minutes[:5])
        suffix = " 等" if len(table.filled_minutes) > 5 else ""
        notice += f"（表中未列出的第 {preview}{suffix} 分钟按 0 人计）"
    return _flash_redirect(f"/imports/{preview_id}", notice)


def _preview_minutes(preview: service.PreviewData) -> list[dict]:
    minutes = []
    waiting = 0
    for index in range(preview.duration):
        waiting += preview.joins[index] - preview.boards[index]
        minutes.append(
            {
                "minute": preview.minute_base + index,
                "joins": preview.joins[index],
                "boards": preview.boards[index],
                "queue": waiting,
            }
        )
    return minutes


@app.get("/imports/{preview_id}", response_class=HTMLResponse)
def preview_page(
    request: Request,
    preview_id: int,
    message: str | None = None,
    level: str = "ok",
):
    try:
        preview = service.get_preview(_db(), preview_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "preview": preview,
            "minutes": _preview_minutes(preview),
            "message": message,
            "level": level,
        },
    )


@app.post("/imports/{preview_id}/confirm")
def confirm_import_form(preview_id: int):
    try:
        outcome = service.confirm_preview(_db(), preview_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.PreviewStaleError as exc:
        return _flash_redirect("/", str(exc), "error")
    if outcome.outcome == "existing":
        return _flash_redirect(
            f"/sessions/{outcome.session_id}", "该预览已建场，返回原场次。"
        )
    return _flash_redirect(
        f"/sessions/{outcome.session_id}", "读头表已按预览建场，结论已生成。"
    )


# ---------------------------------------------------------------- JSON API


class CredentialIn(BaseModel):
    credential_no: str = Field(min_length=1, max_length=100)
    join_minute: int = Field(ge=0)
    board_minute: int = Field(ge=0)
    count: int = Field(default=1, ge=1)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/sessions")
def api_list_sessions():
    return {
        "sessions": [
            {
                "id": s.id,
                "filename": s.filename,
                "minute_base": s.minute_base,
                "last_minute": s.last_minute,
                "total_passengers": s.total_passengers,
                "source": s.source,
                "verified_at": s.verified_at,
                "lower": r.lower,
                "upper": r.upper,
                "status": r.status,
                "status_label": r.status_label,
                "version": r.version,
                "updated_at": r.updated_at,
            }
            for s, r in service.list_sessions(_db())
        ]
    }


@app.post("/api/sessions", status_code=201)
def api_upload_session(file: UploadFile = File(...)):
    filename = file.filename or "upload.csv"
    data = file.file.read()
    try:
        parsed = csv_import.parse_csv(data, filename)
        session_id = service.create_session(_db(), parsed)
    except (csv_import.CsvError, matching.InfeasibleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"整表退回：{exc}") from exc
    return JSONResponse(_session_payload(session_id), status_code=201)


@app.get("/api/sessions/{session_id}")
def api_session_detail(session_id: int):
    try:
        return _session_payload(session_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/sessions/{session_id}/credentials")
def api_add_credential(session_id: int, payload: CredentialIn):
    try:
        outcome = service.add_credential(
            _db(),
            session_id,
            payload.credential_no,
            payload.join_minute,
            payload.board_minute,
            payload.count,
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CredentialConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.CredentialInfeasibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = {
        "outcome": outcome.outcome,
        "credential": {
            "credential_no": outcome.credential.credential_no,
            "join_minute": outcome.credential.join_minute,
            "board_minute": outcome.credential.board_minute,
            "count": outcome.credential.count,
        },
    }
    body.update(_session_payload(session_id)["result"])
    return JSONResponse(body, status_code=201 if outcome.outcome == "applied" else 200)


# ------------------------------------------------------- 读头对照与读头表导入


def _calibration_payload(calibration: service.CalibrationData) -> dict:
    return {
        "id": calibration.id,
        "session_id": calibration.session_id,
        "filename": calibration.filename,
        "minute_base": calibration.minute_base,
        "duration": calibration.duration,
        "readers": list(calibration.readers),
        "assignments": list(calibration.assignments),
        "assignment_count": len(calibration.assignments),
        "created_at": calibration.created_at,
    }


def _preview_payload(preview: service.PreviewData) -> dict:
    return {
        "id": preview.id,
        "filename": preview.filename,
        "minute_base": preview.minute_base,
        "last_minute": preview.last_minute,
        "duration": preview.duration,
        "readers": list(preview.readers),
        "joins": list(preview.joins),
        "boards": list(preview.boards),
        "total_passengers": preview.total_passengers,
        "calibration_id": preview.calibration_id,
        "session_id": preview.session_id,
        "created_at": preview.created_at,
        "confirmed_at": preview.confirmed_at,
    }


@app.post("/api/sessions/{session_id}/verify")
def api_verify_session(session_id: int):
    try:
        session = service.mark_session_verified(_db(), session_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": session.id, "verified_at": session.verified_at}


@app.get("/api/calibrations/current")
def api_current_calibration():
    calibration = service.current_calibration(_db())
    return {
        "calibration": _calibration_payload(calibration) if calibration else None
    }


@app.post("/api/calibrations", status_code=201)
def api_create_calibration(session_id: int = Form(...), file: UploadFile = File(...)):
    filename = file.filename or "readers.csv"
    data = file.file.read()
    try:
        table = readers.parse_reader_csv(data, filename)
        calibration_id = service.create_calibration(_db(), session_id, table)
    except csv_import.CsvError as exc:
        raise HTTPException(status_code=422, detail=f"对照读头表退回：{exc}") from exc
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    calibration = service.current_calibration(_db())
    assert calibration is not None and calibration.id == calibration_id
    return _calibration_payload(calibration)


@app.post("/api/imports/preview", status_code=201)
def api_preview_import(file: UploadFile = File(...)):
    filename = file.filename or "readers.csv"
    data = file.file.read()
    try:
        table = readers.parse_reader_csv(data, filename)
        preview_id = service.create_preview(_db(), table)
    except csv_import.CsvError as exc:
        raise HTTPException(status_code=422, detail=f"读头表退回：{exc}") from exc
    except service.PreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _preview_payload(service.get_preview(_db(), preview_id))


@app.get("/api/imports/{preview_id}")
def api_get_import(preview_id: int):
    try:
        return _preview_payload(service.get_preview(_db(), preview_id))
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/imports/{preview_id}/confirm")
def api_confirm_import(preview_id: int):
    try:
        outcome = service.confirm_preview(_db(), preview_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.PreviewStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    body = {"outcome": outcome.outcome, "preview_id": preview_id}
    body.update(_session_payload(outcome.session_id))
    return JSONResponse(body, status_code=201 if outcome.outcome == "created" else 200)
