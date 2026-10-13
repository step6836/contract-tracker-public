"""Tiny username/password auth for a two-person internal tool."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Optional

import bcrypt

from . import storage

_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt())
_FAILS: dict[str, deque] = defaultdict(deque)
MAX_FAILS = 5
WINDOW_SECONDS = 300


def _load() -> list[dict]:
    return (storage.read_json(storage.USERS_FILE, {}) or {}).get("users", [])


def has_users() -> bool:
    return bool(_load())


def get_user(username: str) -> Optional[dict]:
    username = (username or "").strip().lower()
    return next((u for u in _load() if u["username"] == username), None)


def public(user: dict) -> dict:
    return {"username": user["username"], "display_name": user["display_name"]}


def create_user(username: str, display_name: str, password: str) -> None:
    username = username.strip().lower()
    if not username or not password:
        raise ValueError("Username and password are required")
    if len(password) < 8:
        raise ValueError("Use a password of at least 8 characters")
    users = [u for u in _load() if u["username"] != username]
    users.append({
        "username": username,
        "display_name": display_name.strip() or username,
        "password_hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
    })
    storage.init()
    storage.write_json(storage.USERS_FILE, {"users": users})


def check_rate_limit(key: str) -> bool:
    """True if this key may try to sign in right now."""
    q = _FAILS[key]
    now = time.time()
    while q and now - q[0] > WINDOW_SECONDS:
        q.popleft()
    return len(q) < MAX_FAILS


def record_failure(key: str) -> None:
    _FAILS[key].append(time.time())


def verify(username: str, password: str) -> Optional[dict]:
    user = get_user(username)
    hashed = user["password_hash"].encode() if user else _DUMMY_HASH  # same work either way
    ok = bcrypt.checkpw(password.encode(), hashed)
    return user if (user and ok) else None
