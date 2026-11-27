"""FastAPI 시방서 스마트 뷰어 백엔드 (인증, RBAC, 다중 키워드 검색, 이미지/PDF 서빙, 동기화 번들)."""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, db, search_parser

COOKIE_NAME = "spec_session"
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
SAFE_USERNAME_RE = re.compile(r"^[A-Za-z0-9._가-힣-]{2,40}$")
ALLOWED_TAR_ROOTS = {"specs.db", "thumbs", "previews", "pdfs"}
MAX_SYNC_BYTES = 1024 * 1024 * 1024  # 1 GiB

MAX_RATE_LIMIT_KEYS = 2048
RATE_LIMIT_WINDOW_SEC = 300
RATE_LIMIT_MAX_FAILS = 15

_rate_lock = threading.Lock()
_login_fails: dict[str, list[float]] = {}
_sync_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_state_db()
    db_path = config.get_db_path()
    if db_path.exists():
        try:
            with db.specs_write_conn(db_path) as conn:
                db.ensure_specs_migrations(conn)
        except sqlite3.Error:
            pass
    yield


app = FastAPI(title="Spec Smart Viewer", version="1.0.0", lifespan=lifespan)

config.STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self' data:; connect-src 'self'; object-src 'self' blob:; frame-ancestors 'none'",
    )
    return response


# ── Helpers ────────────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:240]


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    with _rate_lock:
        if len(_login_fails) > MAX_RATE_LIMIT_KEYS:
            stale = [k for k, ts in _login_fails.items() if not ts or ts[-1] < cutoff]
            for k in stale:
                _login_fails.pop(k, None)
            if len(_login_fails) > MAX_RATE_LIMIT_KEYS:
                _login_fails.clear()
        recent = [t for t in _login_fails.get(ip, []) if t >= cutoff]
        if recent:
            _login_fails[ip] = recent
        else:
            _login_fails.pop(ip, None)
        if len(recent) >= RATE_LIMIT_MAX_FAILS:
            raise HTTPException(
                status_code=429,
                detail="로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
            )


def _record_login_fail(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    with _rate_lock:
        recent = [t for t in _login_fails.get(ip, []) if t >= cutoff]
        recent.append(now)
        _login_fails[ip] = recent


def _clear_login_fails(ip: str) -> None:
    with _rate_lock:
        _login_fails.pop(ip, None)


def _validate_id(value: str, label: str = "ID") -> str:
    if not SAFE_ID_RE.match(value or ""):
        raise HTTPException(status_code=400, detail=f"잘못된 {label} 형식입니다.")
    return value


def require_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    session = db.get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="세션이 만료되었습니다.")
    return session


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


def _read_version_payload() -> dict[str, Any]:
    if config.VERSION_FILE.exists():
        try:
            return json.loads(config.VERSION_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "version": "1.0.0",
        "build": "20261127.1",
        "released_at": config.kst_date_str(),
        "name": "시방서 스마트 뷰어",
        "title": "NRD-K2 제어(설비) 시방서 뷰어",
        "releaseNotes": [],
        "features": [],
    }


def _call_worker(method: str, path: str, timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    worker_url = config.get_worker_url()
    if not worker_url:
        raise HTTPException(status_code=503, detail="동기화 워커 URL이 설정되지 않았습니다.")
    token = config.get_sync_token()
    if not token:
        raise HTTPException(status_code=503, detail="SYNC_TOKEN이 설정되지 않았습니다.")

    req = urllib.request.Request(
        f"{worker_url}{path}",
        method=method.upper(),
        headers={"X-Sync-Token": token},
        data=b"" if method.upper() == "POST" else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"detail": err.reason}
        except json.JSONDecodeError:
            payload = {"detail": raw or str(err.reason)}
        return err.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise HTTPException(status_code=502, detail=f"워커 통신 실패: {err}") from err


# ── Request Schemas ────────────────────────────────────────────────────────


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class CreateUserReq(BaseModel):
    username: str = Field(..., min_length=2, max_length=40)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(default="viewer")


class ResetPasswordReq(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)


class FavoriteReq(BaseModel):
    note: str = Field(default="", max_length=500)


class PinReq(BaseModel):
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    note: str = Field(..., min_length=1, max_length=500)


# ── PWA & Shell Routes ─────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def serve_index():
    idx = config.FRONTEND_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"service": "spec-smart-viewer", "status": "ok"})


@app.api_route("/manifest.json", methods=["GET", "HEAD"], include_in_schema=False)
def serve_manifest():
    manifest_file = config.STATIC_DIR / "manifest.json"
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    if manifest_file.exists():
        return FileResponse(
            manifest_file,
            media_type="application/manifest+json",
            headers=headers,
        )
    return JSONResponse(
        {
            "id": "/?pwa=1",
            "name": "시방서 스마트 뷰어",
            "short_name": "시방서뷰어",
            "start_url": "/?pwa=1",
            "display": "standalone",
            "orientation": "portrait",
        },
        headers=headers,
    )


@app.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
def serve_sw():
    sw_file = config.STATIC_DIR / "sw.js"
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Service-Worker-Allowed": "/",
    }
    if sw_file.exists():
        return FileResponse(sw_file, media_type="application/javascript", headers=headers)
    return Response(
        content="self.addEventListener('install',()=>self.skipWaiting());",
        media_type="application/javascript",
        headers=headers,
    )


@app.get("/api/version")
def get_version():
    return JSONResponse(
        _read_version_payload(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/healthz")
def healthz():
    db_path = config.get_db_path()
    specs_exists = db_path.exists()
    pages_count = 0
    if specs_exists:
        try:
            with db.specs_conn() as conn:
                row = conn.execute("SELECT COUNT(*) FROM pages").fetchone()
                pages_count = int(row[0]) if row else 0
        except sqlite3.Error:
            specs_exists = False
    return {
        "status": "ok",
        "kst": config.now_kst_iso(),
        "specs_db_exists": specs_exists,
        "pages_count": pages_count,
    }


# ── Auth Routes ────────────────────────────────────────────────────────────


@app.post("/api/auth/login")
def login(payload: LoginReq, request: Request, response: Response):
    ip = _client_ip(request)
    ua = _user_agent(request)
    _check_rate_limit(ip)

    username = payload.username.strip()
    with db.state_conn() as conn:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not row or not db.verify_password(payload.password, row["password_hash"]):
        _record_login_fail(ip)
        db.record_audit(username or None, "LOGIN_FAILED", username, ip, ua, "invalid credentials")
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    _clear_login_fails(ip)
    token = db.create_session(row["username"], row["role"], ip, ua)
    db.record_audit(row["username"], "LOGIN_OK", row["role"], ip, ua)

    secure_flag = _is_https(request) and config.is_cookie_secure()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
        max_age=db.SESSION_TTL_SECONDS,
        path="/",
    )
    return {"username": row["username"], "role": row["role"], "kst": config.now_kst_iso()}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = db.get_session(token)
        if session:
            db.record_audit(session["username"], "LOGOUT", "", _client_ip(request), _user_agent(request))
        db.delete_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(require_user)):
    return {
        "username": user["username"],
        "role": user["role"],
        "kst": config.now_kst_iso(),
        "date": config.kst_date_str(),
    }


# ── Spec Browsing & Search Routes ──────────────────────────────────────────


def _specs_ready() -> bool:
    return config.get_db_path().exists()


@app.get("/api/meta")
def get_meta(_user: dict[str, Any] = Depends(require_user)):
    if not _specs_ready():
        return {"db_ready": False, "meta": {}, "total_docs": 0, "total_pages": 0, "total_ocr_pages": 0}
    with db.specs_conn() as conn:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM documents) AS total_docs,
                (SELECT COUNT(*) FROM pages) AS total_pages,
                (SELECT COUNT(*) FROM pages WHERE page_type = 'ocr') AS total_ocr_pages,
                (SELECT COUNT(*) FROM sections) AS total_headings
            """
        ).fetchone()
    return {
        "db_ready": True,
        "meta": meta,
        "total_docs": counts["total_docs"],
        "total_pages": counts["total_pages"],
        "total_ocr_pages": counts["total_ocr_pages"],
        "total_headings": counts["total_headings"],
    }


@app.get("/api/docs")
def list_docs(_user: dict[str, Any] = Depends(require_user)):
    if not _specs_ready():
        return {"items": [], "db_ready": False}
    with db.specs_conn() as conn:
        rows = conn.execute(
            """
            SELECT doc_id, kh_code, spec_code, title, rev_date, source_file, sha256,
                   total_pages, heading_count, ocr_pages
            FROM documents
            ORDER BY spec_code ASC, doc_id ASC
            """
        ).fetchall()
    return {"items": [dict(r) for r in rows], "db_ready": True}


@app.get("/api/toc")
def get_toc(
    doc_id: str = Query(default=""),
    _user: dict[str, Any] = Depends(require_user),
):
    if not _specs_ready():
        return {"items": []}
    clauses: list[str] = []
    params: list[Any] = []
    if doc_id.strip():
        _validate_id(doc_id.strip(), "문서 ID")
        clauses.append("s.doc_id = ?")
        params.append(doc_id.strip())
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.specs_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.doc_id, d.spec_code, s.section_no, s.title, s.code_ref,
                   s.level, s.start_page, s.end_page, s.page_id
            FROM sections s
            JOIN documents d ON d.doc_id = s.doc_id
            {where_sql}
            ORDER BY d.spec_code ASC, s.id ASC
            """,
            params,
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/pages")
def search_spec_pages(
    q: str = Query(default="", max_length=400),
    doc_ids: str = Query(default=""),
    page_type: str = Query(default=""),
    body_only: bool = Query(default=False),
    scope: str = Query(default="page"),
    fav_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=5000),
    limit: int = Query(default=40, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
):
    if not _specs_ready():
        return {"total": 0, "page": page, "limit": limit, "terms": [], "items": [], "db_ready": False}

    parsed_doc_ids = []
    if doc_ids.strip():
        for part in doc_ids.split(","):
            val = part.strip()
            if val:
                parsed_doc_ids.append(_validate_id(val, "문서 ID"))

    if page_type not in ("", "cover", "toc", "history", "diagram", "appendix", "body", "text", "ocr"):
        raise HTTPException(status_code=400, detail="허용되지 않은 page_type입니다.")
    if scope not in ("page", "doc"):
        raise HTTPException(status_code=400, detail="허용되지 않은 scope입니다.")

    page_ids_filter: set[str] | None = None
    if fav_only:
        with db.state_conn() as sconn:
            fav_rows = sconn.execute(
                "SELECT page_id FROM favorites WHERE username = ?",
                (user["username"],),
            ).fetchall()
        page_ids_filter = {r["page_id"] for r in fav_rows}

    with db.specs_conn() as conn:
        result = search_parser.search_pages(
            conn,
            q=q,
            doc_ids=parsed_doc_ids or None,
            page_type=page_type,
            body_only=body_only,
            scope=scope,
            page_ids_filter=page_ids_filter,
            page=page,
            limit=limit,
        )
    result["db_ready"] = True
    return result


@app.get("/api/pages/{page_id}")
def get_page_detail(
    page_id: str,
    q: str = Query(default="", max_length=400),
    user: dict[str, Any] = Depends(require_user),
):
    _validate_id(page_id, "페이지 ID")
    if not _specs_ready():
        raise HTTPException(status_code=404, detail="시방서 인덱스가 아직 준비되지 않았습니다.")

    with db.specs_conn() as conn:
        row = conn.execute(
            """
            SELECT p.page_id, p.doc_id, p.page_no, p.total_pages, p.page_type,
                   p.section_no, p.section_title, p.width_px, p.height_px,
                   p.sha256, p.has_preview, p.has_pdf, p.ocr_status,
                   p.clean_text, p.tokens_json,
                   d.kh_code, d.spec_code, d.title AS doc_title, d.rev_date, d.source_file
            FROM pages p
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE p.page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="페이지를 찾을 수 없습니다.")

        prev_row = conn.execute(
            "SELECT page_id FROM pages WHERE doc_id = ? AND page_no < ? ORDER BY page_no DESC LIMIT 1",
            (row["doc_id"], row["page_no"]),
        ).fetchone()
        next_row = conn.execute(
            "SELECT page_id FROM pages WHERE doc_id = ? AND page_no > ? ORDER BY page_no ASC LIMIT 1",
            (row["doc_id"], row["page_no"]),
        ).fetchone()
        sec_rows = conn.execute(
            """
            SELECT id, section_no, title, code_ref, level, start_page, end_page, page_id
            FROM sections
            WHERE doc_id = ?
            ORDER BY start_page ASC, id ASC
            """,
            (row["doc_id"],),
        ).fetchall()

    highlight_boxes: list[dict[str, Any]] = []
    positive_terms: list[str] = []
    if q.strip():
        ast = search_parser.parse_query(q)
        positive_terms = search_parser.extract_positive_terms(ast)
        if positive_terms:
            try:
                tokens = json.loads(row["tokens_json"] or "[]")
            except json.JSONDecodeError:
                tokens = []
            highlight_boxes = search_parser.match_token_boxes(tokens, positive_terms)

    with db.state_conn() as sconn:
        fav = sconn.execute(
            "SELECT note, created_at FROM favorites WHERE username = ? AND page_id = ?",
            (user["username"], page_id),
        ).fetchone()
        pin_rows = sconn.execute(
            """
            SELECT id, page_id, username, x, y, note, created_at
            FROM page_pins
            WHERE page_id = ?
            ORDER BY id ASC
            """,
            (page_id,),
        ).fetchall()

    return {
        "page_id": row["page_id"],
        "doc_id": row["doc_id"],
        "kh_code": row["kh_code"],
        "spec_code": row["spec_code"],
        "doc_title": row["doc_title"],
        "rev_date": row["rev_date"],
        "source_file": row["source_file"],
        "page_no": row["page_no"],
        "total_pages": row["total_pages"],
        "page_type": row["page_type"],
        "section_no": row["section_no"],
        "section_title": row["section_title"],
        "width_px": row["width_px"],
        "height_px": row["height_px"],
        "sha256": row["sha256"],
        "v": (row["sha256"] or "")[:12],
        "has_preview": bool(row["has_preview"]),
        "has_pdf": bool(row["has_pdf"]),
        "ocr_status": row["ocr_status"],
        "clean_text": row["clean_text"],
        "terms": positive_terms,
        "highlight_boxes": highlight_boxes,
        "siblings": {
            "prev_page_id": prev_row["page_id"] if prev_row else None,
            "next_page_id": next_row["page_id"] if next_row else None,
        },
        "sections": [dict(s) for s in sec_rows],
        "is_favorite": fav is not None,
        "favorite_note": fav["note"] if fav else "",
        "pins": [dict(p) for p in pin_rows],
    }


# ── Media Delivery ─────────────────────────────────────────────────────────


def _serve_page_asset(
    page_id: str,
    folder: str,
    ext: str,
    media_type: str,
    v: str,
    inline_pdf: bool = False,
) -> FileResponse:
    _validate_id(page_id, "페이지 ID")
    data_dir = config.get_data_dir().resolve()
    target = (data_dir / folder / f"{page_id}.{ext}").resolve()
    if target.parent != (data_dir / folder).resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="요청한 장표 자산을 찾을 수 없습니다.")

    page_sha = ""
    if _specs_ready():
        try:
            with db.specs_conn() as conn:
                r = conn.execute("SELECT sha256 FROM pages WHERE page_id = ?", (page_id,)).fetchone()
                if r:
                    page_sha = r["sha256"] or ""
        except sqlite3.Error:
            pass

    headers: dict[str, str] = {}
    if page_sha and v and (v == page_sha or v == page_sha[:12]):
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
        headers["ETag"] = f'"{page_sha}"'
    else:
        headers["Cache-Control"] = "private, no-cache"
    if inline_pdf:
        headers["Content-Disposition"] = f'inline; filename="{page_id}.pdf"'

    return FileResponse(target, media_type=media_type, headers=headers)


@app.get("/api/pages/{page_id}/thumb.webp")
def get_page_thumb(
    page_id: str,
    v: str = Query(default=""),
    _user: dict[str, Any] = Depends(require_user),
):
    return _serve_page_asset(page_id, "thumbs", "webp", "image/webp", v)


@app.get("/api/pages/{page_id}/preview.webp")
def get_page_preview(
    page_id: str,
    v: str = Query(default=""),
    _user: dict[str, Any] = Depends(require_user),
):
    return _serve_page_asset(page_id, "previews", "webp", "image/webp", v)


@app.get("/api/pages/{page_id}/pdf")
def get_page_pdf(
    page_id: str,
    v: str = Query(default=""),
    _user: dict[str, Any] = Depends(require_user),
):
    return _serve_page_asset(page_id, "pdfs", "pdf", "application/pdf", v, inline_pdf=True)


# ── Favorites & Pins ───────────────────────────────────────────────────────


@app.get("/api/favorites")
def list_favorites(user: dict[str, Any] = Depends(require_user)):
    with db.state_conn() as sconn:
        favs = sconn.execute(
            "SELECT page_id, note, created_at FROM favorites WHERE username = ? ORDER BY created_at DESC",
            (user["username"],),
        ).fetchall()
    if not favs:
        return {"items": []}

    page_meta: dict[str, dict[str, Any]] = {}
    if _specs_ready():
        ids = [f["page_id"] for f in favs]
        placeholders = ",".join("?" for _ in ids)
        with db.specs_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT p.page_id, p.doc_id, p.page_no, p.total_pages, p.page_type,
                       p.section_no, p.section_title, p.sha256, d.spec_code, d.title AS doc_title
                FROM pages p
                JOIN documents d ON d.doc_id = p.doc_id
                WHERE p.page_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            page_meta = {r["page_id"]: dict(r) for r in rows}

    items = []
    for f in favs:
        item = {"page_id": f["page_id"], "note": f["note"], "created_at": f["created_at"]}
        if f["page_id"] in page_meta:
            item.update(page_meta[f["page_id"]])
            item["v"] = (page_meta[f["page_id"]].get("sha256") or "")[:12]
        items.append(item)
    return {"items": items}


@app.post("/api/favorites/{page_id}")
def add_favorite(
    page_id: str,
    payload: FavoriteReq,
    user: dict[str, Any] = Depends(require_user),
):
    _validate_id(page_id, "페이지 ID")
    if _specs_ready():
        with db.specs_conn() as conn:
            exists = conn.execute("SELECT 1 FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="페이지를 찾을 수 없습니다.")
    with db.state_conn() as sconn:
        sconn.execute(
            """
            INSERT INTO favorites(username, page_id, note, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username, page_id) DO UPDATE SET note = excluded.note, created_at = excluded.created_at
            """,
            (user["username"], page_id, payload.note.strip(), config.now_kst_iso()),
        )
    return {"status": "ok", "page_id": page_id}


@app.delete("/api/favorites/{page_id}")
def delete_favorite(page_id: str, user: dict[str, Any] = Depends(require_user)):
    _validate_id(page_id, "페이지 ID")
    with db.state_conn() as sconn:
        sconn.execute(
            "DELETE FROM favorites WHERE username = ? AND page_id = ?",
            (user["username"], page_id),
        )
    return {"status": "ok", "page_id": page_id}


@app.post("/api/pages/{page_id}/pins", status_code=201)
def create_pin(
    page_id: str,
    payload: PinReq,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
):
    _validate_id(page_id, "페이지 ID")
    if _specs_ready():
        with db.specs_conn() as conn:
            if not conn.execute("SELECT 1 FROM pages WHERE page_id = ?", (page_id,)).fetchone():
                raise HTTPException(status_code=404, detail="페이지를 찾을 수 없습니다.")

    now = config.now_kst_iso()
    with db.state_conn() as sconn:
        cur = sconn.execute(
            """
            INSERT INTO page_pins(page_id, username, x, y, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (page_id, user["username"], payload.x, payload.y, payload.note.strip(), now),
        )
        pin_id = cur.lastrowid

    db.record_audit(
        user["username"],
        "PIN_CREATE",
        page_id,
        _client_ip(request),
        _user_agent(request),
        payload.note.strip()[:120],
    )
    return {
        "id": pin_id,
        "page_id": page_id,
        "username": user["username"],
        "x": payload.x,
        "y": payload.y,
        "note": payload.note.strip(),
        "created_at": now,
    }


@app.delete("/api/pages/{page_id}/pins/{pin_id}")
def delete_pin(
    page_id: str,
    pin_id: int,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
):
    _validate_id(page_id, "페이지 ID")
    with db.state_conn() as sconn:
        row = sconn.execute(
            "SELECT id, username FROM page_pins WHERE id = ? AND page_id = ?",
            (pin_id, page_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="핀을 찾을 수 없습니다.")
        if row["username"] != user["username"] and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="다른 사용자의 핀은 삭제할 수 없습니다.")
        sconn.execute("DELETE FROM page_pins WHERE id = ?", (pin_id,))

    db.record_audit(user["username"], "PIN_DELETE", f"{page_id}#{pin_id}", _client_ip(request), _user_agent(request))
    return {"status": "ok", "id": pin_id}


# ── Admin User & Audit Routes ──────────────────────────────────────────────


@app.get("/api/admin/users")
def admin_list_users(_admin: dict[str, Any] = Depends(require_admin)):
    with db.state_conn() as sconn:
        rows = sconn.execute(
            "SELECT username, role, created_at FROM users ORDER BY role ASC, username ASC"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/users", status_code=201)
def admin_create_user(
    payload: CreateUserReq,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
):
    username = payload.username.strip()
    if not SAFE_USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="사용자명은 2~40자의 영문/숫자/한글/._-만 허용됩니다.")
    if payload.role not in ("admin", "engineer", "viewer"):
        raise HTTPException(status_code=400, detail="역할은 admin, engineer 또는 viewer만 허용됩니다.")

    pw_hash = db.hash_password(payload.password)
    now = config.now_kst_iso()
    try:
        with db.state_conn() as sconn:
            sconn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, pw_hash, payload.role, now),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="이미 존재하는 사용자입니다.") from exc

    db.record_audit(admin["username"], "USER_CREATE", username, _client_ip(request), _user_agent(request), payload.role)
    return {"username": username, "role": payload.role, "created_at": now}


@app.patch("/api/admin/users/{username}/password")
def admin_reset_password(
    username: str,
    payload: ResetPasswordReq,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
):
    pw_hash = db.hash_password(payload.password)
    with db.state_conn() as sconn:
        cur = sconn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (pw_hash, username),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        sconn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    db.invalidate_session_cache(username=username)
    db.record_audit(admin["username"], "USER_PW_RESET", username, _client_ip(request), _user_agent(request))
    return {"status": "ok", "username": username}


@app.delete("/api/admin/users/{username}")
def admin_delete_user(
    username: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
):
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="현재 로그인한 본인 계정은 삭제할 수 없습니다.")
    with db.state_conn() as sconn:
        cur = sconn.execute("DELETE FROM users WHERE username = ?", (username,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        sconn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    db.invalidate_session_cache(username=username)
    db.record_audit(admin["username"], "USER_DELETE", username, _client_ip(request), _user_agent(request))
    return {"status": "ok", "username": username}


@app.get("/api/admin/audit-logs")
def admin_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    action: str = Query(default=""),
    _admin: dict[str, Any] = Depends(require_admin),
):
    with db.state_conn() as sconn:
        if action.strip():
            rows = sconn.execute(
                """
                SELECT id, at_kst, username, action, target, ip, user_agent, detail
                FROM audit_logs
                WHERE action = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (action.strip(), limit),
            ).fetchall()
        else:
            rows = sconn.execute(
                """
                SELECT id, at_kst, username, action, target, ip, user_agent, detail
                FROM audit_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ── Admin Sync Bundle & Worker Proxy ───────────────────────────────────────


def _validate_tar_member(member: tarfile.TarInfo) -> Path:
    raw_name = member.name.replace("\\", "/")
    while raw_name.startswith("./"):
        raw_name = raw_name[2:]
    if not raw_name or raw_name.startswith("/") or "\\" in member.name:
        raise HTTPException(status_code=400, detail=f"허용되지 않은 압축 경로: {member.name}")
    parts = Path(raw_name).parts
    if not parts or ".." in parts or parts[0] not in ALLOWED_TAR_ROOTS:
        raise HTTPException(status_code=400, detail=f"허용되지 않은 압축 대상: {member.name}")
    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
        raise HTTPException(status_code=400, detail=f"심볼릭/특수 파일은 허용되지 않습니다: {member.name}")
    return Path(*parts)


def _extract_sync_bundle(tar_path: Path, data_dir: Path) -> dict[str, Any]:
    stage_dir = data_dir / ".sync_stage"
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    extracted_files = 0
    try:
        with tarfile.open(tar_path, mode="r:gz") as tf:
            members = tf.getmembers()
            safe_pairs: list[tuple[tarfile.TarInfo, Path]] = []
            for m in members:
                rel = _validate_tar_member(m)
                dest = (stage_dir / rel).resolve()
                if stage_dir.resolve() not in dest.parents and dest != stage_dir.resolve():
                    raise HTTPException(status_code=400, detail="압축 경로 탈출 시도가 차단되었습니다.")
                safe_pairs.append((m, dest))

            for m, dest in safe_pairs:
                if m.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    with src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out, length=1024 * 128)
                    extracted_files += 1

        staged_db = stage_dir / "specs.db"
        if not staged_db.is_file():
            raise HTTPException(status_code=400, detail="번들에 specs.db가 포함되어 있지 않습니다.")

        # 무결성 및 페이지 수 확인
        try:
            check_conn = sqlite3.connect(f"file:{staged_db}?mode=ro", uri=True)
            pages_count = int(check_conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
            check_conn.close()
        except sqlite3.Error as err:
            raise HTTPException(status_code=400, detail=f"specs.db 검증 실패: {err}") from err

        # 자산 디렉토리 원자적/안전 교체
        for folder in ("thumbs", "previews", "pdfs"):
            staged_sub = stage_dir / folder
            target_sub = data_dir / folder
            if staged_sub.is_dir():
                old_sub = data_dir / f".{folder}.old"
                if old_sub.exists():
                    shutil.rmtree(old_sub, ignore_errors=True)
                if target_sub.exists():
                    os.replace(target_sub, old_sub)
                os.replace(staged_sub, target_sub)
                if old_sub.exists():
                    shutil.rmtree(old_sub, ignore_errors=True)
            else:
                target_sub.mkdir(parents=True, exist_ok=True)

        # DB 교체 (DELETE journal 모드이므로 os.replace만으로 안전 교체 가능)
        live_db = data_dir / "specs.db"
        tmp_db = data_dir / ".specs.db.incoming"
        shutil.copy2(staged_db, tmp_db)
        os.replace(tmp_db, live_db)
        for suffix in ("-wal", "-shm", "-journal"):
            side = data_dir / f"specs.db{suffix}"
            if side.exists():
                side.unlink()

        return {"pages_count": pages_count, "extracted_files": extracted_files}
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


@app.post("/api/admin/sync")
async def receive_sync_bundle(request: Request):
    expected = config.get_sync_token()
    if not expected:
        raise HTTPException(status_code=503, detail="SYNC_TOKEN이 설정되지 않았습니다.")
    provided = request.headers.get("x-sync-token", "")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="유효하지 않은 동기화 토큰입니다.")

    data_dir = config.get_data_dir()
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="다른 동기화 번들 반영이 진행 중입니다.")

    tmp_tar: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(data_dir), prefix=".sync_", suffix=".tar.gz", delete=False) as tf:
            tmp_tar = Path(tf.name)
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_SYNC_BYTES:
                    raise HTTPException(status_code=413, detail="동기화 번들 크기 제한을 초과했습니다.")
                tf.write(chunk)

        try:
            result = _extract_sync_bundle(tmp_tar, data_dir)
        except tarfile.TarError as err:
            raise HTTPException(status_code=400, detail=f"유효하지 않은 tar.gz 형식: {err}") from err

        db.record_audit("sync-bundle", "SYNC_APPLIED", "specs.db", _client_ip(request), _user_agent(request), json.dumps(result))
        return {"status": "ok", "kst": config.now_kst_iso(), **result}
    finally:
        if tmp_tar and tmp_tar.exists():
            tmp_tar.unlink(missing_ok=True)
        _sync_lock.release()


@app.get("/api/admin/sync/status")
def admin_sync_status(_admin: dict[str, Any] = Depends(require_admin)):
    status_code, payload = _call_worker("GET", "/status")
    return JSONResponse(payload, status_code=status_code)


@app.post("/api/admin/sync/trigger")
def admin_sync_trigger(request: Request, admin: dict[str, Any] = Depends(require_admin)):
    status_code, payload = _call_worker("POST", "/trigger")
    db.record_audit(admin["username"], "SYNC_TRIGGER", "", _client_ip(request), _user_agent(request))
    return JSONResponse(payload, status_code=status_code)


@app.post("/api/admin/sync/test-alert")
def admin_sync_test_alert(request: Request, admin: dict[str, Any] = Depends(require_admin)):
    status_code, payload = _call_worker("POST", "/test-alert")
    db.record_audit(admin["username"], "SYNC_TEST_ALERT", "", _client_ip(request), _user_agent(request))
    return JSONResponse(payload, status_code=status_code)
