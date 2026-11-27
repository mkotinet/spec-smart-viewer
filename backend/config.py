"""환경 설정 및 KST(Asia/Seoul) 표준 시각 도우미."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def now_kst_iso() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S KST")


def kst_date_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
VERSION_FILE = BASE_DIR / "version.json"


def get_data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path() -> Path:
    return get_data_dir() / "specs.db"


def get_auth_db_path() -> Path:
    """인증·세션·감사로그·메모핀은 시방서 인덱스 교체 시에도 유지되도록 별도 DB에 영속 저장."""
    return get_data_dir() / "app_state.db"


def get_admin_user() -> str:
    return os.environ.get("ADMIN_USER", "admin").strip() or "admin"


def get_admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def get_sync_token() -> str:
    return os.environ.get("SYNC_TOKEN", "")


def get_worker_url() -> str:
    return os.environ.get("SYNC_WORKER_URL", "").strip().rstrip("/")


def is_cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")
