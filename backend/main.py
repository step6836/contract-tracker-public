"""Contracts Tracker API + static frontend.  Run:  python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth, engine, publish, storage
from .engine import EngineError, nice, parse

storage.init()


@asynccontextmanager
async def lifespan(_: FastAPI):
    publish.publish_in_background()  # a slow or offline Box Drive must not delay startup
    yield


app = FastAPI(title="Contracts Tracker", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=storage.get_secret(), session_cookie="ct_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=False)


@app.exception_handler(EngineError)
def engine_error(_: Request, exc: EngineError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---- request bodies (field names avoid clashing with the `date` type) ------------------------

# The side track (the Expansion lease, the Renewal inspection); "lease" is what older pages send.
Track = Literal["main", "side", "lease"]


class LoginIn(BaseModel):
    username: str
    password: str


class Unit(BaseModel):
    store_number: str = ""
    store_name: str = ""


class CaseIn(BaseModel):
    type: str
    store_number: str
    store_name: str
    franchisee: str = ""
    transferee: str = ""
    related_units: str = ""
    units: list[Unit] = []
    dates: dict[str, Optional[date]] = {}
    start_date: date
    target_signing_date: Optional[date] = None
    flags: dict[str, bool] = {}
    box_url: str = ""
    links: list[str] = []


class CaseUpdate(BaseModel):
    store_name: Optional[str] = None
    franchisee: Optional[str] = None
    transferee: Optional[str] = None
    related_units: Optional[str] = None
    units: Optional[list[Unit]] = None
    dates: Optional[dict[str, Optional[date]]] = None
    box_url: Optional[str] = None
    start_date: Optional[date] = None
    target_signing_date: Optional[date] = None
    lease_deadline: Optional[date] = None
    flags: Optional[dict[str, bool]] = None


class CompleteIn(BaseModel):
    completed_on: Optional[date] = None
    track: Track = "main"


class SkipIn(BaseModel):
    reason: str
    track: Track = "main"


class CheckIn(BaseModel):
    key: str
    checked: bool
    checked_on: Optional[date] = None
    track: Track = "main"


class SectionIn(BaseModel):
    section: str
    node: str
    value: Optional[str] = None      # for a choice
    item: Optional[str] = None       # for a checkbox
    checked: bool = True
    done_on: Optional[date] = None   # used if this completes the section
    track: Track = "main"


class CorrectionIn(BaseModel):
    on: Optional[date] = None
    track: Track = "main"


class IssueIn(BaseModel):
    section: str
    item: str
    checked: bool
    on: Optional[date] = None        # used if this clears the last issue
    track: Track = "main"


class TrackIn(BaseModel):
    track: Track = "main"


class ScheduleIn(BaseModel):
    on: date
    at: str                          # "HH:MM", 24-hour
    track: Track = "main"


class HappenedIn(BaseModel):
    happened: bool
    track: Track = "main"


class StepDateIn(BaseModel):
    new_date: date
    track: Track = "main"


class NoteIn(BaseModel):
    text: str
    follow_up_date: Optional[date] = None


class CloseIn(BaseModel):
    outcome: Literal["not_approved", "withdrawn"]
    reason: str


class HoldIn(BaseModel):
    reason: str
    on: Optional[date] = None


class ResumeIn(BaseModel):
    on: Optional[date] = None


class LinkIn(BaseModel):
    case_id: str
    linked: bool = True


class ReminderIn(BaseModel):
    key: str
    done: bool
    on: Optional[date] = None


# ---- helpers ------------------------------------------------------------------------------

def current_user(request: Request) -> dict:
    user = auth.get_user(request.session.get("user", ""))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def _load(case_id: str):
    case = storage.load_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    tmpl = engine.TEMPLATES.get(case["type"])
    if not tmpl:
        raise HTTPException(500, f"Unknown case type '{case['type']}'.")
    engine.sync(case, tmpl)
    return case, tmpl


def _summaries(cfg) -> list[dict]:
    """Every case's list summary, before relations are added."""
    out = []
    for c in storage.list_cases():
        tmpl = engine.TEMPLATES.get(c["type"])
        if not tmpl:
            continue
        engine.sync(c, tmpl)
        out.append(engine.summarize(engine.build_view(c, tmpl, config=cfg)))
    return out


def _named(rel: dict) -> dict:
    """Who opened each related case, by display name, so you can see whose case it is."""
    for row in rel["linked"] + rel["suggested"]:
        rec = auth.get_user(row["created_by"])
        row["created_by_name"] = rec["display_name"] if rec else row["created_by"]
    return rel


def _view(case, tmpl):
    cfg = storage.load_config()
    view = engine.build_view(case, tmpl, config=cfg)
    others = [s for s in _summaries(cfg) if s["id"] != case["id"]]
    return engine.add_relations(view, _named(engine.relations(engine.summarize(view), others)))


def _save_and_log(case, user, action, text, detail=None):
    storage.save_case(case)
    storage.log_event(user["username"], action, case["id"], {"text": text, **(detail or {})})
    publish.schedule()


def _label(case, tmpl, step_id, track="main"):
    steps = case.get("side_steps", []) if track != "main" else case["steps"]  # labels can depend on the case's flags
    return next((s["label"] for s in steps if s["id"] == step_id), step_id)


# ---- routes ---------------------------------------------------------------------------------

api = APIRouter(prefix="/api")


@api.post("/login")
def login(body: LoginIn, request: Request):
    key = f"{request.client.host if request.client else '?'}|{body.username.strip().lower()}"
    if not auth.check_rate_limit(key):
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
    user = auth.verify(body.username, body.password)
    if not user:
        auth.record_failure(key)
        raise HTTPException(401, "Wrong username or password.")
    request.session["user"] = user["username"]
    return auth.public(user)


@api.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@api.get("/me")
def me(user=Depends(current_user)):
    return auth.public(user)


@api.get("/meta")
def meta(user=Depends(current_user)):
    return {"today": engine.iso(engine.today()),
            "templates": {k: {"label": t["label"], "start_label": t.get("start_label"),
                              "start_label_when": t.get("start_label_when"), "flags": t.get("flags", []),
                              "notes": t.get("notes", []), "has_lease_track": bool(t.get("side_track")),
                              "dates": t.get("dates", []), "target_from": t.get("target_from"),
                              "has_transferee": k == "transfer", "links_to": t.get("links_to", []),
                              "has_signing": any(s.get("milestone") == "signing" for s in t["steps"])}
                          for k, t in engine.TEMPLATES.items()}}


@api.get("/cases")
def list_cases(user=Depends(current_user)):
    summaries = _summaries(storage.load_config())
    return [engine.add_relations(s, _named(engine.relations(s, summaries))) for s in summaries]


@api.post("/cases")
def create_case(body: CaseIn, user=Depends(current_user)):
    tmpl = engine.TEMPLATES.get(body.type)
    if not tmpl:
        raise HTTPException(400, "Unknown case type.")
    with storage.LOCK:
        others = [_load(i)[0] for i in dict.fromkeys(body.links)]
        cid = storage.next_case_id(tmpl["id_prefix"], body.store_number)
        case = engine.new_case(tmpl, cid, body.model_dump(), user["username"])
        for o in others:
            engine.set_link(case, tmpl, o, True)
        _save_and_log(case, user, "case_created",
                      f"opened {tmpl['label'].lower()} case {cid} for {case['store_name']}")
        for o in others:
            storage.log_event(user["username"], "case_linked", o["id"], {"text": f"linked {tmpl['label'].lower()} case {cid}"})
    return _view(case, tmpl)


@api.get("/cases/{case_id}")
def get_case(case_id: str, user=Depends(current_user)):
    case, tmpl = _load(case_id)
    return _view(case, tmpl)


@api.patch("/cases/{case_id}")
def update_case(case_id: str, body: CaseUpdate, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        changed = engine.update_case(case, tmpl, body.model_dump(exclude_unset=True))
        if changed:
            _save_and_log(case, user, "case_updated", "updated " + ", ".join(c.replace("_", " ") for c in changed))
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/complete")
def complete(case_id: str, step_id: str, body: CompleteIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.completed_on or engine.today()
        engine.complete_step(case, tmpl, step_id, when, user["username"], body.track)
        _save_and_log(case, user, "step_completed",
                      f"completed \"{_label(case, tmpl, step_id, body.track)}\" ({nice(when)})",
                      {"step_id": step_id, "date": engine.iso(when)})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/skip")
def skip(case_id: str, step_id: str, body: SkipIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.skip_step(case, tmpl, step_id, body.reason, user["username"], body.track)
        _save_and_log(case, user, "step_skipped",
                      f"skipped \"{_label(case, tmpl, step_id, body.track)}\": {body.reason.strip()}",
                      {"step_id": step_id})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/check")
def check(case_id: str, step_id: str, body: CheckIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.checked_on or engine.today()
        item, completed = engine.set_check(case, tmpl, step_id, body.key, body.checked, when, user["username"], body.track)
        step = _label(case, tmpl, step_id, body.track)
        text = (f"checked \"{item}\" on \"{step}\" ({nice(when)})" if body.checked
                else f"unchecked \"{item}\" on \"{step}\"")
        if completed:
            text += f", completing \"{step}\""
        _save_and_log(case, user, "step_completed" if completed else "step_checked", text,
                      {"step_id": step_id, "key": body.key, "date": engine.iso(when) if body.checked else None})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/section")
def section(case_id: str, step_id: str, body: SectionIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        text, completed = engine.section_action(case, tmpl, step_id, body.section, body.node, body.value, body.item,
                                                body.checked, body.done_on or engine.today(), user["username"], body.track)
        step = _label(case, tmpl, step_id, body.track)
        if completed:
            text += f", completing \"{step}\""
        _save_and_log(case, user, "step_completed" if completed else "step_section", f"{step}: {text}",
                      {"step_id": step_id, "section": body.section})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/correction")
def correction(case_id: str, step_id: str, body: CorrectionIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.on or engine.today()
        back = engine.request_correction(case, tmpl, step_id, when, user["username"], body.track)
        _save_and_log(case, user, "correction_started",
                      f"sent the case back from \"{_label(case, tmpl, step_id, body.track)}\" to \"{back}\" for correction ({nice(when)})",
                      {"step_id": step_id})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/issue")
def issue(case_id: str, step_id: str, body: IssueIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.on or engine.today()
        text, resolved = engine.set_issue(case, tmpl, step_id, body.section, body.item, body.checked, when,
                                          user["username"], body.track)
        step = _label(case, tmpl, step_id, body.track)
        if resolved:
            text += f"; all issues cleared ({nice(when)}), \"{step}\" is done again"
        _save_and_log(case, user, "correction_resolved" if resolved else "correction_issue", f"{step}: {text}",
                      {"step_id": step_id, "section": body.section, "item": body.item})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/schedule")
def schedule(case_id: str, step_id: str, body: ScheduleIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        label, when = engine.schedule_step(case, tmpl, step_id, body.on, body.at, user["username"], body.track)
        engine.sync(case, tmpl)  # the review step finishes right away if its review is already over
        _save_and_log(case, user, "scheduled", f"scheduled the {label.lower()} for {engine.nice_at(when)}",
                      {"step_id": step_id, "at": when.isoformat(timespec="minutes")})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/happened")
def happened(case_id: str, step_id: str, body: HappenedIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = engine.answer_scheduled(case, tmpl, step_id, body.happened, user["username"], body.track)
        step = _label(case, tmpl, step_id, body.track)
        states = case.get("side_steps", []) if body.track != "main" else case["steps"]
        done = next((s["status"] == "done" for s in states if s["id"] == step_id), False)
        if body.happened:  # a step with more to do after it (e.g. the interview's evaluation) stays open
            _save_and_log(case, user, "step_completed" if done else "step_checked",
                          f"confirmed \"{step}\" happened ({engine.nice_at(when)})",
                          {"step_id": step_id, "date": engine.iso(when.date())})
        else:
            _save_and_log(case, user, "schedule_missed",
                          f"said \"{step}\" didn't happen on {engine.nice_at(when)}; it needs a new date",
                          {"step_id": step_id})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/undo")
def undo(case_id: str, body: TrackIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        was = engine.undo_last(case, tmpl, user["username"], body.track)
        _save_and_log(case, user, "step_undone", f"undid \"{was['label']}\"", {"step_id": was["id"]})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/date")
def change_date(case_id: str, step_id: str, body: StepDateIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.set_step_date(case, tmpl, step_id, body.new_date, body.track)
        _save_and_log(case, user, "step_date_changed",
                      f"changed the date on \"{_label(case, tmpl, step_id, body.track)}\" to {nice(body.new_date)}",
                      {"step_id": step_id})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.add_note(case, body.text, body.follow_up_date, user["username"], tmpl)
        extra = f" (follow up {nice(body.follow_up_date)})" if body.follow_up_date else ""
        _save_and_log(case, user, "note_added", "added a note" + extra)
    return _view(case, tmpl)


@api.post("/cases/{case_id}/notes/{note_id}/resolve")
def resolve_note(case_id: str, note_id: str, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.resolve_note(case, note_id, user["username"])
        _save_and_log(case, user, "note_resolved", "marked a follow-up as done")
    return _view(case, tmpl)


@api.post("/cases/{case_id}/close")
def close(case_id: str, body: CloseIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.close_case(case, body.outcome, body.reason)
        _save_and_log(case, user, "case_closed", f"closed the case as {body.outcome.replace('_', ' ')}: {body.reason.strip()}")
    return _view(case, tmpl)


@api.post("/cases/{case_id}/hold")
def hold(case_id: str, body: HoldIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.on or engine.today()
        at = engine.put_on_hold(case, tmpl, body.reason, when, user["username"])
        _save_and_log(case, user, "case_on_hold", f"put the case on hold at \"{at}\" ({nice(when)}): {body.reason.strip()}")
    return _view(case, tmpl)


@api.post("/cases/{case_id}/resume")
def resume(case_id: str, body: ResumeIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.on or engine.today()
        h = engine.resume(case, when, user["username"])
        days = (when - engine.parse(h["start"])).days
        _save_and_log(case, user, "case_resumed",
                      f"took the case off hold ({nice(when)}); it was on hold at \"{h['step_label']}\" for {days} day{'s' if days != 1 else ''}")
    return _view(case, tmpl)


@api.post("/cases/{case_id}/steps/{step_id}/reminder")
def reminder(case_id: str, step_id: str, body: ReminderIn, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        when = body.on or engine.today()
        label = engine.set_reminder(case, tmpl, step_id, body.key, body.done, when, user["username"])
        _save_and_log(case, user, "reminder", f"{'marked' if body.done else 'unmarked'} \"{label}\" as done"
                      + (f" ({nice(when)})" if body.done else ""), {"step_id": step_id, "key": body.key})
    return _view(case, tmpl)


@api.post("/cases/{case_id}/links")
def link(case_id: str, body: LinkIn, user=Depends(current_user)):
    """Link or unlink two cases, from either side. The link is kept on the one that links (the lease)."""
    with storage.LOCK:
        case, tmpl = _load(case_id)
        other, other_tmpl = _load(body.case_id)
        if not tmpl.get("links_to") and other_tmpl.get("links_to"):
            case, tmpl, other, other_tmpl = other, other_tmpl, case, tmpl
        if not tmpl.get("links_to"):
            raise EngineError(f"Only {', '.join(t['label'].lower() for t in engine.TEMPLATES.values() if t.get('links_to'))} cases can be linked to other cases.")
        engine.set_link(case, tmpl, other, body.linked)
        verb = "linked" if body.linked else "unlinked"
        _save_and_log(case, user, f"case_{verb}", f"{verb} {other_tmpl['label'].lower()} case {other['id']}")
        storage.log_event(user["username"], f"case_{verb}", other["id"], {"text": f"{verb} {tmpl['label'].lower()} case {case['id']}"})
    case, tmpl = _load(case_id)
    return _view(case, tmpl)


@api.post("/cases/{case_id}/reopen")
def reopen(case_id: str, user=Depends(current_user)):
    with storage.LOCK:
        case, tmpl = _load(case_id)
        engine.reopen_case(case)
        _save_and_log(case, user, "case_reopened", "reopened the case")
    return _view(case, tmpl)


@api.post("/publish")
def publish_report(user=Depends(current_user)):
    return publish.publish_now()  # never raises; a failure comes back as {"ok": false, "detail": ...}


@api.get("/events")
def events(case_id: Optional[str] = None, limit: int = 60, user=Depends(current_user)):
    names = {}
    out = []
    for e in storage.read_events(case_id, min(limit, 500)):
        u = e["user"]
        if u not in names:
            rec = auth.get_user(u)
            names[u] = rec["display_name"] if rec else u
        out.append({**e, "display_name": names[u]})
    return out


app.include_router(api)

# ---- serve the built frontend (frontend/dist) ---------------------------------------------------

DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        target = (DIST / path).resolve()
        if path and target.is_file() and DIST.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(DIST / "index.html")
