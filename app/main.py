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

from . import csv_import, matching, service
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
    items = [
        {
            "session": session,
            "result": result,
            "explanation": service.describe_status(session, result),
        }
        for session, result in service.list_sessions(_db())
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "items": items,
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
