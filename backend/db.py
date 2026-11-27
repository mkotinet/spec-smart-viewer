"""SQLite 영속 저장소: 시방서 FTS5/Trigram 인덱스 DB + 인증/세션/감사로그 DB 분리 관리."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from backend.config import (
    KST,
    get_admin_password,
    get_admin_user,
    get_auth_db_path,
    get_db_path,
    now_kst,
    now_kst_iso,
)

MAX_AUDIT_LOGS = 5000
PBKDF2_ITERS = 120_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 90  # 90일 슬라이딩 만료
SESSION_TOUCH_INTERVAL_S = 300           # 5분 쓰기 디바운싱
SESSION_CACHE_TTL_S = 60.0               # 60초 인메모리 캐시
MAX_SESSION_CACHE = 512                  # 256MB 컨테이너 메모리 상한 보호

_session_cache_lock = threading.Lock()
_session_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invalidate_session_cache(token: str | None = None, username: str | None = None) -> None:
    with _session_cache_lock:
        if token is None and username is None:
            _session_cache.clear()
            return
        if token is not None:
            _session_cache.pop(_hash_session_token(token), None)
        if username is not None:
            dead = [k for k, (_, sess) in _session_cache.items() if sess.get("username") == username]
            for k in dead:
                _session_cache.pop(k, None)


def _parse_kst_iso(ts: str) -> datetime | None:
    try:
        clean = (ts or "").replace(" KST", "").strip()
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


def _tune(conn: sqlite3.Connection, read_only: bool = False, journal_mode: str = "WAL") -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-16000")      # 16MB 상한 (DS920+ RAM 보호)
    conn.execute("PRAGMA temp_store=MEMORY")
    if not read_only:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")


@contextmanager
def state_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_auth_db_path(), timeout=10)
    _tune(conn, read_only=False, journal_mode="WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def specs_conn() -> Iterator[sqlite3.Connection]:
    """시방서 인덱스 DB 순수 읽기 전용 커넥션 (DELETE 저널 전제, -wal/-shm 생성 원천 차단)."""
    path = get_db_path()
    if not path.is_file():
        init_specs_db(path)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    _tune(conn, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def specs_write_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """기동·동기화 마이그레이션 전용 쓰기 커넥션 (종료 시 DELETE 저널 모드 강제 유지)."""
    target = path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15)
    _tune(conn, read_only=False, journal_mode="DELETE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        for suffix in ("-wal", "-shm"):
            target.with_name(f"{target.name}{suffix}").unlink(missing_ok=True)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters_s))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def _ensure_trigram_table(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages_trigram USING fts5("
            "page_id UNINDEXED, doc_id UNINDEXED, search_blob, tokenize='trigram')"
        )
        return True
    except sqlite3.OperationalError:
        return False


def init_specs_db(path: Path | None = None) -> None:
    target = path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    _tune(conn, read_only=False, journal_mode="DELETE")
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                kh_code TEXT NOT NULL DEFAULT '',
                spec_code TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                rev_date TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                total_pages INTEGER NOT NULL DEFAULT 0,
                heading_count INTEGER NOT NULL DEFAULT 0,
                ocr_pages INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                total_pages INTEGER NOT NULL,
                page_type TEXT NOT NULL DEFAULT 'body',
                section_no TEXT NOT NULL DEFAULT '',
                section_title TEXT NOT NULL DEFAULT '',
                width_px INTEGER NOT NULL DEFAULT 0,
                height_px INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL,
                has_preview INTEGER NOT NULL DEFAULT 1,
                has_pdf INTEGER NOT NULL DEFAULT 0,
                ocr_status TEXT NOT NULL DEFAULT '',
                clean_text TEXT NOT NULL DEFAULT '',
                tokens_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pages_doc_no ON pages(doc_id, page_no);
            CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type);
            CREATE INDEX IF NOT EXISTS idx_pages_sha ON pages(sha256);

            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT NOT NULL,
                section_no TEXT NOT NULL,
                title TEXT NOT NULL,
                code_ref TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1,
                start_page INTEGER NOT NULL,
                end_page INTEGER NOT NULL,
                page_id TEXT NOT NULL,
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sections_doc ON sections(doc_id, start_page, id);

            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                page_id UNINDEXED,
                doc_id UNINDEXED,
                spec_code,
                doc_title,
                section_no,
                section_title,
                clean_text,
                tokenize='unicode61 remove_diacritics 0'
            );
        """)
        _ensure_trigram_table(conn)
        conn.commit()
    finally:
        conn.close()
        for suffix in ("-wal", "-shm"):
            target.with_name(f"{target.name}{suffix}").unlink(missing_ok=True)


def ensure_specs_migrations(conn: sqlite3.Connection) -> None:
    """기존 specs.db 무중단 인덱스/테이블 보강."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_doc_no ON pages(doc_id, page_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_doc ON sections(doc_id, start_page, id)")
    _ensure_trigram_table(conn)


def init_state_db() -> None:
    with state_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'engineer',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                user_agent TEXT,
                ip TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS favorites (
                username TEXT NOT NULL,
                page_id TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (username, page_id)
            );

            CREATE TABLE IF NOT EXISTS page_pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id TEXT NOT NULL,
                username TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pins_page ON page_pins(page_id);

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at_kst TEXT NOT NULL,
                username TEXT,
                action TEXT NOT NULL,
                target TEXT,
                ip TEXT,
                user_agent TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_id ON audit_logs(id DESC);
        """)
        admin_user = get_admin_user()
        admin_pw = get_admin_password()
        if admin_pw:
            row = conn.execute(
                "SELECT username, password_hash, role FROM users WHERE username = ?",
                (admin_user,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                    (admin_user, hash_password(admin_pw), now_kst_iso()),
                )
            elif row["role"] != "admin" or not verify_password(admin_pw, row["password_hash"]):
                conn.execute(
                    "UPDATE users SET password_hash = ?, role = 'admin' WHERE username = ?",
                    (hash_password(admin_pw), admin_user),
                )
                invalidate_session_cache(username=admin_user)


def record_audit(username: str | None, action: str, target: str = "",
                 ip: str = "", user_agent: str = "", detail: str = "") -> None:
    with state_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs(at_kst, username, action, target, ip, user_agent, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_kst_iso(), username, action, target, ip, (user_agent or "")[:200], (detail or "")[:400]),
        )
        conn.execute(
            "DELETE FROM audit_logs WHERE id <= (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (MAX_AUDIT_LOGS,),
        )


def create_session(username: str, role: str, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(token)
    now = now_kst_iso()
    with state_conn() as conn:
        conn.execute(
            "INSERT INTO sessions(token, username, role, user_agent, ip, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, username, role, (user_agent or "")[:200], ip, now, now),
        )
    return token


def get_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    token_hash = _hash_session_token(token)
    mono_now = time.monotonic()
    with _session_cache_lock:
        hit = _session_cache.get(token_hash)
        if hit is not None:
            exp_mono, cached = hit
            if exp_mono > mono_now:
                return dict(cached)
            _session_cache.pop(token_hash, None)

    with state_conn() as conn:
        row = conn.execute(
            "SELECT token, username, role, created_at, last_used_at FROM sessions "
            "WHERE token = ? OR token = ?",
            (token_hash, token),
        ).fetchone()
        if row is None:
            return None
        stored_key = row["token"]
        now = now_kst()
        last_dt = _parse_kst_iso(row["last_used_at"])
        if last_dt is not None:
            age_s = (now - last_dt).total_seconds()
            if age_s > SESSION_TTL_SECONDS:
                conn.execute("DELETE FROM sessions WHERE token = ?", (stored_key,))
                return None
            if stored_key != token_hash:
                conn.execute(
                    "UPDATE sessions SET token = ?, last_used_at = ? WHERE token = ?",
                    (token_hash, now_kst_iso(), stored_key),
                )
            elif age_s >= SESSION_TOUCH_INTERVAL_S:
                conn.execute(
                    "UPDATE sessions SET last_used_at = ? WHERE token = ?",
                    (now_kst_iso(), stored_key),
                )
        else:
            conn.execute(
                "UPDATE sessions SET token = ?, last_used_at = ? WHERE token = ?",
                (token_hash, now_kst_iso(), stored_key),
            )
        sess = {
            "username": row["username"],
            "role": row["role"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    with _session_cache_lock:
        if len(_session_cache) >= MAX_SESSION_CACHE:
            expired = [k for k, (e, _) in _session_cache.items() if e <= mono_now]
            for k in expired:
                _session_cache.pop(k, None)
            while len(_session_cache) >= MAX_SESSION_CACHE:
                _session_cache.pop(next(iter(_session_cache)))
        _session_cache[token_hash] = (mono_now + SESSION_CACHE_TTL_S, sess)
    return dict(sess)


def delete_session(token: str) -> None:
    if not token:
        return
    token_hash = _hash_session_token(token)
    invalidate_session_cache(token=token)
    with state_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ? OR token = ?", (token_hash, token))
