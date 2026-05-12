from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def auth_config() -> dict[str, Any]:
    """JWT settings from environment."""
    secret = (os.environ.get("JWT_SECRET") or "").strip()
    if not secret:
        secret = "dev-insecure-change-JWT_SECRET"
    exp_h = os.environ.get("JWT_EXPIRE_HOURS", "").strip()
    try:
        exp_hours = int(exp_h) if exp_h else 336
    except ValueError:
        exp_hours = 336
    return {"secret_key": secret, "algorithm": "HS256", "exp_hours": exp_hours}


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd.verify(password, password_hash)
    except Exception:
        return False


def create_token(*, user_id: int, email: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or auth_config()
    now = datetime.now(timezone.utc)
    exp = int((now + timedelta(hours=int(cfg["exp_hours"]))).timestamp())
    payload = {"sub": str(int(user_id)), "email": str(email), "exp": exp}
    return jwt.encode(payload, cfg["secret_key"], algorithm=str(cfg["algorithm"]))


def decode_token(token: str, cfg: dict[str, Any] | None = None) -> tuple[int, str]:
    cfg = cfg or auth_config()
    payload = jwt.decode(token, cfg["secret_key"], algorithms=[str(cfg["algorithm"])])
    uid = int(str(payload.get("sub")))
    email = str(payload.get("email") or "")
    return uid, email
