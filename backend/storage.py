"""Plain-JSON storage: one file per case, an append-only event log, and a users file."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRACKER_DATA_DIR", ROOT / "data"))
CASES_DIR = DATA_DIR / "cases"
EVENTS_FILE = DATA_DIR / "events.jsonl"
USERS_FILE = DATA_DIR / "users.json"
SECRET_FILE = DATA_DIR / "secret.key"
CONFIG_FILE = DATA_DIR / "config.json"

# One process serves the app, so one re-entrant lock is enough to keep read-modify-write safe.
LOCK = threading.RLock()

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def init() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj) -> None:
    """Write via a temp file and rename so a crash can never leave a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):  # Windows can briefly lock files (antivirus, indexer)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def get_secret() -> str:
    init()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_FILE.write_text(key)
    return key


def load_config() -> dict:
    cfg = {"default_stale_days": 7}
    cfg.update(read_json(CONFIG_FILE, {}) or {})
    return cfg


# ---- cases ----------------------------------------------------------------

def _case_path(case_id: str) -> Path:
    if not _SAFE_ID.match(case_id):
        raise ValueError("Bad case id")
    return CASES_DIR / f"{case_id}.json"


def load_case(case_id: str) -> Optional[dict]:
    try:
        return read_json(_case_path(case_id))
    except ValueError:
        return None


def save_case(case: dict) -> None:
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        init()
        write_json(_case_path(case["id"]), case)


def list_cases() -> list[dict]:
    init()
    out = []
    for p in sorted(CASES_DIR.glob("*.json")):
        c = read_json(p)
        if c:
            out.append(c)
    return out


def next_case_id(prefix: str, store_number: str) -> str:
    """IDs look like E-3-1: type prefix, store number, then a running number for that store."""
    store = re.sub(r"[^A-Za-z0-9]", "", store_number) or "0"
    init()
    highest = 0
    for p in CASES_DIR.glob(f"{prefix}-{store}-*.json"):
        m = re.match(rf"^{re.escape(prefix)}-{re.escape(store)}-(\d+)$", p.stem)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}-{store}-{highest + 1}"


# ---- event log ------------------------------------------------------------

def log_event(user: str, action: str, case_id: Optional[str] = None, detail: Optional[dict] = None) -> None:
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user": user,
        "action": action,
        "case_id": case_id,
        "detail": detail or {},
    }
    with LOCK:
        init()
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_events(case_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    out = []
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if case_id is None or rec.get("case_id") == case_id:
                out.append(rec)
    out.reverse()
    return out[:limit]
