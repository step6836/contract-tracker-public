"""Workflow engine: templates -> cases, strict step order, timing, and 'needs attention' logic.

Everything here is a pure function of (case, template, today) so a future scheduler
(email digest, reminders) can call build_view()/attention without going through the web app.
"""
from __future__ import annotations

import calendar
import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_STALE_DAYS = 7
LEVEL_RANK = {"ready": 1, "warn": 2, "alert": 3}


class EngineError(ValueError):
    """A user-facing problem (bad date, wrong order...). The message is shown in the UI."""


def load_templates() -> dict[str, dict]:
    out = {}
    for p in sorted(TEMPLATE_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        out[t["type"]] = t
    return out


TEMPLATES = load_templates()


# ---- small helpers ----------------------------------------------------------

def today() -> date:
    return date.today()


def parse(d: Optional[str]) -> Optional[date]:
    return date.fromisoformat(d) if d else None


def iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def nice(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def nice_at(dt: datetime) -> str:
    return f"{nice(dt.date())} at {dt.strftime('%I:%M %p').lstrip('0')}"


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- step state ---------------------------------------------------------------

def _blank(step: dict) -> dict:
    return {"id": step["id"], "label": step["label"], "status": "pending",
            "date": None, "by": None, "reason": None, "auto": False}


def _cond_skips(step: dict, flags: dict, held_at: frozenset = frozenset()) -> bool:
    if step.get("only_if_held_at") and step["only_if_held_at"] not in held_at:
        return True
    cond = step.get("skip_when")
    return bool(cond) and bool(flags.get(cond["flag"], False)) == cond["equals"]


def _skip_reason(step: dict) -> str:
    if step.get("only_if_held_at"):
        return "Not needed: the case wasn't put on hold there"
    return step["skip_when"].get("reason", "Not needed")


def _applies(cond: Optional[dict], flags: dict) -> bool:
    """A template condition like {"flag": "new_location", "equals": false}; no condition always applies."""
    return not cond or bool(flags.get(cond["flag"], False)) == cond["equals"]


def start_label(tmpl: dict, flags: dict) -> Optional[str]:
    """The first step's name, which can depend on a flag ("start_label_when"), e.g. a new location's lease."""
    cond = tmpl.get("start_label_when")
    return cond["label"] if cond and _applies(cond, flags) else tmpl.get("start_label")


def _label(step: dict, flags: dict) -> str:
    """A step's label, which can depend on a flag ("label_when"), e.g. no transferee to evaluate."""
    cond = step.get("label_when")
    if cond and bool(flags.get(cond["flag"], False)) == cond["equals"]:
        return cond["label"]
    return step["label"]


def _sync_track(existing: list[dict], tpl_steps: list[dict], flags: dict, held_at: frozenset = frozenset()) -> list[dict]:
    """Line a case's saved step states up with the current template (safe if the template changes)."""
    by_id = {s["id"]: s for s in existing}
    out = []
    for step in tpl_steps:
        st = by_id.pop(step["id"], None) or _blank(step)
        st["label"] = _label(step, flags)
        out.append(st)
        if st["status"] == "pending" and _cond_skips(step, flags, held_at):
            st.update(status="skipped", auto=True, reason=_skip_reason(step))
        elif st["status"] == "skipped" and st.get("auto") and not _cond_skips(step, flags, held_at):
            st.update(status="pending", auto=False, reason=None)
    for st in by_id.values():  # steps that were removed from the template but have history
        if st["status"] != "pending" and not st.get("auto"):
            st["legacy"] = True
            out.append(st)
    return out


def _all_finished(states: list[dict]) -> bool:
    return all(s["status"] in ("done", "skipped") for s in states)


def _recompute_status(case: dict) -> None:
    if case["status"] not in ("active", "completed"):
        return
    finished = _all_finished(case["steps"]) and _all_finished(case.get("side_steps", []))
    if finished and case["status"] == "active":
        case["status"], case["completed_at"] = "completed", iso(today())
    elif not finished and case["status"] == "completed":
        case["status"], case["completed_at"] = "active", None


def _move_between_tracks(case: dict, tmpl: dict) -> None:
    """A step the template moved to the other track (e.g. the renewal inspection, which now runs alongside
    the main steps) takes its saved state with it."""
    main_ids = {s["id"] for s in tmpl["steps"]}
    side_ids = {s["id"] for s in (tmpl.get("side_track") or {}).get("steps", [])}
    for src, dst, ids in (("steps", "side_steps", side_ids - main_ids), ("side_steps", "steps", main_ids - side_ids)):
        moving = [s for s in case.get(src, []) if s["id"] in ids]
        if moving:
            case[src] = [s for s in case[src] if s["id"] not in ids]
            have = {s["id"] for s in case.get(dst, [])}
            case[dst] = case.get(dst, []) + [s for s in moving if s["id"] not in have]


def sync(case: dict, tmpl: dict) -> dict:
    flags = {f["key"]: f.get("default", False) for f in tmpl.get("flags", [])}
    flags.update({k: v for k, v in case.get("flags", {}).items() if k in flags})
    case["flags"] = flags
    _move_between_tracks(case, tmpl)
    case["steps"] = _sync_track(case.get("steps", []), tmpl["steps"], flags, _held_at(case))
    side = tmpl.get("side_track")
    case["side_steps"] = _sync_track(case.get("side_steps", []), side["steps"], flags) if side else []
    _finish_booked_review(case, tmpl)
    _recompute_status(case)
    return case


def _current_index(states: list[dict]) -> Optional[int]:
    return next((i for i, s in enumerate(states) if s["status"] == "pending"), None)


# ---- the side track: steps that run next to the main ones -------------------------------------------
# A template's "side_track" (the Expansion lease, the Renewal inspection) has its own steps in strict order.
# It starts with the case, or, with "starts_after": "<main step id>", once that main step and every one
# before it is done or skipped. "alongside": true makes it part of the case's timing: its current step is
# timed and flagged when stale, and lists show it next to the main step.
# The API calls it track "side" ("lease" is still accepted from older pages).

def _side_gate(case: dict, tmpl: dict) -> tuple[bool, Optional[date]]:
    """Whether the side track has started, and the day it did."""
    after = (tmpl.get("side_track") or {}).get("starts_after")
    start = parse(case["start_date"])
    ids = [s["id"] for s in case["steps"]]
    if not after or after not in ids:
        return True, start
    upto = case["steps"][:ids.index(after) + 1]
    if not _all_finished(upto):
        return False, None
    return True, max([start] + [parse(s["date"]) for s in upto if s["status"] == "done" and s["date"]])


def _waiting_label(tmpl: dict) -> str:
    """What a case is doing once its main steps are done but the side track isn't, e.g. "Waiting on lease"."""
    side = tmpl.get("side_track") or {}
    return side.get("waiting_label") or f"Waiting on {side.get('label', 'lease').lower()}"


def _side_started(case: dict) -> bool:
    """Anything recorded on the side track yet."""
    return any(s["status"] != "pending" or any((s.get("checks") or {}).values()) for s in case.get("side_steps", []))


def _track(case: dict, tmpl: dict, track: str, started: bool = True):
    """A track's step states. Changing the side track before it has started (`started`) is refused."""
    if track != "main":
        side = tmpl.get("side_track")
        if not side:
            raise EngineError("This case type has no second track.")
        if started and not _side_gate(case, tmpl)[0]:
            raise EngineError(side.get("starts_note") or f"The {side['label']} hasn't started yet.")
        return case["side_steps"]
    return case["steps"]


def _for_flags(step: dict, flags: Optional[dict]) -> dict:
    """The step as it applies to a case: checklist groups with "only_when" are dropped (with their
    boxes) when the case's flag doesn't match, e.g. no background check for an existing franchisee."""
    if flags is None or not any(g.get("only_when") for g in step.get("groups", [])):
        return step
    on = lambda g: not g.get("only_when") or bool(flags.get(g["only_when"]["flag"], False)) == g["only_when"]["equals"]
    groups = [g for g in step["groups"] if on(g)]
    keys = {g["key"] for g in groups}
    items = [c for c in step.get("checklist", []) if c.get("group") is None or c["group"] in keys]
    return {**step, "groups": groups, "checklist": items}


def _tpl_step(tmpl: dict, step_id: str, track: str, flags: Optional[dict] = None) -> dict:
    return _for_flags(next((s for s in _tpl_steps(tmpl, track) if s["id"] == step_id), {}), flags)


def _clock_start(case: dict, states: list[dict], idx: int, track: str, tmpl: Optional[dict] = None) -> date:
    """When the step at idx began: the date the last finished step before it was done (on the side track,
    no earlier than the day the track started)."""
    d = parse(case["start_date"])
    if track != "main" and tmpl:
        d = _side_gate(case, tmpl)[1] or d
    for s in states[:idx]:
        if s["status"] == "done" and s["date"]:
            d = max(d, parse(s["date"]))
    return d


def _entered(case: dict, states: list[dict], idx: int, track: str = "main", tmpl: Optional[dict] = None) -> date:
    """When the step at idx began; the clock restarts when a step is sent back for correction."""
    st = states[idx]
    return parse(st["correction"]["started"]) if st.get("correction") else _clock_start(case, states, idx, track, tmpl)


def _started_by_date(step: dict, states: list[dict]) -> Optional[date]:
    """A step can be started by a box on an earlier step (e.g. the FA draft being sent to the franchisee
    starts the review period). Returns that box's date once the earlier step is done; with several boxes
    ("items", e.g. sent to the transferor and to the transferee), the date the last of them was checked."""
    sb = step.get("started_by")
    src = next((s for s in states if s["id"] == sb["step"]), None) if sb else None
    if not src or src["status"] != "done":
        return None
    dates = [_check_date(src.get("checks", {}), k) for k in sb.get("items") or [sb["item"]]]
    return max(dates) if all(dates) else None


def _review_end(case: dict, step: dict, states: list[dict], idx: int) -> Optional[date]:
    """The day a step's review period is over, or None if it has none (or it hasn't started yet).
    The period counts from a checklist box ("review_from", e.g. the day the FDD was sent), else from
    the box that started the step ("started_by"), else from when the step began."""
    rd = step.get("review_days")
    if not rd:
        return None
    start = (_check_date(states[idx].get("checks", {}), step["review_from"]) if step.get("review_from")
             else _started_by_date(step, states) or _entered(case, states, idx))
    return date.fromordinal(start.toordinal() + rd) if start else None


def _all_holds(case: dict) -> list[dict]:
    """Every hold on the case, finished ones first, then the open one (which has no "end")."""
    return case.get("holds", []) + ([case["hold"]] if case.get("hold") else [])


def _held_at(case: dict) -> frozenset:
    """The steps the case has ever been put on hold at."""
    return frozenset(h["step_id"] for h in _all_holds(case) if h.get("step_id"))


def _held_days(case: dict, since: date, t: date) -> int:
    """Days between `since` and `t` that the case spent on hold; they don't count toward days in step."""
    total = 0
    for h in _all_holds(case):
        start, end = max(parse(h["start"]), since), min(parse(h["end"]) if h.get("end") else t, t)
        total += max((end - start).days, 0)
    return total


def _require_open(case: dict) -> None:
    if case["status"] not in ("active",):
        raise EngineError(f"This case is {case['status'].replace('_', ' ')}. Reopen it to make changes.")


# ---- creating and changing cases ----------------------------------------------

def _units(rows) -> list[dict]:
    """An expansion's related units, one {store_number, store_name} per unit; blank rows are dropped."""
    out = []
    for r in rows or []:
        number, name = (r.get("store_number") or "").strip(), (r.get("store_name") or "").strip()
        if not number and not name:
            continue
        if number and not number.isdigit():
            raise EngineError("A related unit's Store # can only contain numbers")
        out.append({"store_number": number, "store_name": name})
    return out


def _dates(tmpl: dict, given: Optional[dict], current: Optional[dict] = None, flags: Optional[dict] = None,
           old_flags: Optional[dict] = None) -> dict:
    """A case's own dates from the template's "dates" (e.g. FA term expires). Required ones have to be given
    on a new case and can't be cleared later (cases from before a date was added can still be edited).
    A date with "only_when" isn't required when the flag doesn't match (a new location's lease has no expiration),
    and has to be given once a flag change makes it apply (a new location switched to an existing one)."""
    out = dict(current or {})
    for d in tmpl.get("dates", []):
        if given is not None and d["key"] in given:
            out[d["key"]] = iso(given[d["key"]])
        if not _applies(d.get("only_when"), flags or {}):
            continue
        newly = old_flags is not None and not _applies(d.get("only_when"), old_flags)
        if d.get("required") and not out.get(d["key"]) and (current is None or current.get(d["key"]) or newly):
            raise EngineError(f"{d['label']} is required.")
    return out


def new_case(tmpl: dict, case_id: str, data: dict, user: str) -> dict:
    start: date = data["start_date"]
    if start > today():
        raise EngineError("The start date can't be in the future.")
    store_number = (data.get("store_number") or "").strip()
    store_name = (data.get("store_name") or "").strip()
    if not store_number or not store_name:
        raise EngineError("Store # and store name are required.")
    if not store_number.isdigit():
        raise EngineError("Store # can only contain numbers")
    given = data.get("flags") or {}
    flags = {f["key"]: bool(given.get(f["key"], f.get("default", False))) for f in tmpl.get("flags", [])}
    dates = _dates(tmpl, data.get("dates") or {}, flags=flags)
    # The target signing date can default to one of the case's dates (e.g. when the FA term expires).
    target = data.get("target_signing_date") or parse(dates.get(tmpl.get("target_from", "")))
    case = {
        "id": case_id,
        "type": tmpl["type"],
        "store_number": store_number,
        "store_name": store_name,
        "franchisee": (data.get("franchisee") or "").strip(),
        "transferee": (data.get("transferee") or "").strip(),
        "related_units": (data.get("related_units") or "").strip(),
        "units": _units(data.get("units")),
        "status": "active",
        "closed_reason": None,
        "completed_at": None,
        "start_date": iso(start),
        "target_signing_date": iso(target),
        "dates": dates,
        "hold": None,
        "holds": [],
        "lease_deadline": None,
        "box_url": (data.get("box_url") or "").strip(),
        "links": [],
        "flags": flags,
        "steps": [],
        "side_steps": [],
        "notes": [],
        "created_by": user,
        "created_at": _now(),
        "updated_at": _now(),
    }
    sync(case, tmpl)
    # The first step is the official start of the case, so it is done on the start date, unless it has
    # boxes to tick first (e.g. Management notification); then it finishes, on the start date, once they are.
    if not tmpl["steps"][0].get("checklist"):
        case["steps"][0].update(status="done", date=iso(start), by=user)
    _recompute_status(case)
    return case


def complete_step(case: dict, tmpl: dict, step_id: str, when: date, user: str, track: str = "main") -> dict:
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    if idx is None:
        raise EngineError("There's nothing left to complete here.")
    if states[idx]["id"] != step_id:
        raise EngineError(f"Steps go in order. \"{states[idx]['label']}\" comes next.")
    if when > today():
        raise EngineError("The completion date can't be in the future.")
    floor = _clock_start(case, states, idx, track, tmpl)
    if when < floor:
        raise EngineError(f"That's before the previous step was completed ({nice(floor)}).")
    checks = states[idx].get("checks", {})
    step = _tpl_step(tmpl, step_id, track, case["flags"])
    missing =[_item_name(step, c) for c in step.get("checklist", []) if not checks.get(c["key"])]
    missing += [s["label"] for s in _needed(step) if not _section_done(s, states[idx])]
    if missing:
        raise EngineError(f"Check off {' and '.join(missing)} before completing this step.")
    states[idx].update(status="done", date=iso(when), by=user, reason=None)
    _recompute_status(case)
    return states[idx]


def _check_date(checks: dict, key: str) -> Optional[date]:
    val = checks.get(key)
    return parse(val) if isinstance(val, str) else None  # older cases stored plain True


def _request_clock(step: dict, st: dict) -> Optional[date]:
    """For a step with "stale_after_request_days" (the inspections): each group's clock starts the day its
    "starts_clock" box (Requested) is checked. Returns the oldest still-open request, None if there's none."""
    checks, items = st.get("checks", {}), step.get("checklist", [])
    started = [_check_date(checks, c["key"]) for c in items if c.get("starts_clock") and checks.get(c["key"])
               and not all(checks.get(o["key"]) for o in _group_of(items, c))]
    return min(filter(None, started), default=None)


def _group_of(items: list[dict], item: dict) -> list[dict]:
    """The items in the same checklist group (items without a group form one group), in order."""
    return [c for c in items if c.get("group") == item.get("group")]


def _item_name(step: dict, item: dict) -> str:
    """'Accounting Team Inspection: Requested' for grouped items, plain label otherwise."""
    group = next((g["label"] for g in step.get("groups", []) if g["key"] == item.get("group")), None)
    return f"{group}: {item['label']}" if group else item["label"]


def _closing_item(items: list[dict], checks: dict) -> Optional[dict]:
    """The checked box that finished the step: the latest-dated last item of a group."""
    ends = [g[-1] for g in {c.get("group"): _group_of(items, c) for c in items}.values() if checks.get(g[-1]["key"])]
    return max(ends, key=lambda c: (_check_date(checks, c["key"]) or date.min, items.index(c)), default=None)


def set_check(case: dict, tmpl: dict, step_id: str, key: str, checked: bool, when: date, user: str,
              track: str = "main") -> tuple[str, bool]:
    """Tick (on a date) or untick a checklist item on the current step. Items go in order within their
    group, and once every box is ticked the step completes on the latest of their dates.
    Returns (item name, whether the step completed)."""
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    if idx is None or states[idx]["id"] != step_id:
        raise EngineError("Only the current step's checklist can be changed.")
    _require_not_correcting(states[idx])
    step = _tpl_step(tmpl, step_id, track, case["flags"])
    items = step.get("checklist", [])
    item = next((c for c in items if c["key"] == key), None)
    if item is None:
        raise EngineError(f"Unknown checklist item: {key}")
    group = _group_of(items, item)
    pos = group.index(item)
    checks = states[idx].setdefault("checks", {})
    name = _item_name(step, item)
    # A box can also wait on a box in another group ("after"), e.g. sending the draft to each side waits
    # on the draft being received, while the two sends can go in either order.
    after = next((c for c in items if c["key"] == item.get("after")), None)
    if not checked:
        if any(checks.get(c["key"]) for c in group[pos + 1:]):
            raise EngineError(f"Uncheck {_item_name(step, group[pos + 1])} first.")
        if waiting := next((c for c in items if c.get("after") == key and checks.get(c["key"])), None):
            raise EngineError(f"Uncheck {_item_name(step, waiting)} first.")
        if _sections_started(states[idx]):
            raise EngineError(f"Clear what's been recorded below before unchecking {name}.")
        checks[key] = None
        return name, False
    if item.get("from_schedule"):
        raise EngineError(f"Answer \"{step.get('scheduled_question', 'Did it happen?')}\" once the time has passed.")
    if pos > 0 and not checks.get(group[pos - 1]["key"]):
        raise EngineError(f"Check off {_item_name(step, group[pos - 1])} first.")
    if after and not checks.get(after["key"]):
        raise EngineError(f"Check off {_item_name(step, after)} first.")
    if when > today():
        raise EngineError("The date can't be in the future.")
    floor = _clock_start(case, states, idx, track, tmpl)
    if when < floor:
        raise EngineError(f"That's before the previous step was completed ({nice(floor)}).")
    prev_item = group[pos - 1] if pos > 0 else after
    prev = _check_date(checks, prev_item["key"]) if prev_item else None
    if prev and when < prev:
        raise EngineError(f"That's before {_item_name(step, prev_item)} ({nice(prev)}).")
    checks[key] = iso(when)
    booked = step.get("scheduled_question") and not _held_item(step)
    if _step_ready(step, states[idx]) and not booked:
        # The start of the case stays dated on the start date; its boxes keep their own dates.
        done_on = parse(case["start_date"]) if idx == 0 and track == "main" else _ready_date(step, states[idx])
        complete_step(case, tmpl, step_id, done_on, user, track)
        return name, True
    return name, False


# ---- step sections: parts of one step worked on side by side (e.g. the FAI documents) ------------
# A section is a small flow of nodes starting at "start": a "choice" node (pick an option, which
# names the next node, or none to finish) or a "checks" node (tick every box, then go on to "next").
# Reaching the end completes the section on the date given. The sections open once the step's own
# checklist is done (e.g. FAI Sent), and the step completes once every section is complete.

def _walk(sec: dict, sst: dict) -> tuple[list[str], Optional[str]]:
    """Follow a section's answers from the start: (nodes on the path, node waiting on input or None if done)."""
    path, key = [], sec["start"]
    answers, checks = sst.get("answers", {}), sst.get("checks", {})
    while key:
        path.append(key)
        node = sec["nodes"][key]
        if node["type"] == "choice":
            opt = next((o for o in node["options"] if o["value"] == answers.get(key)), None)
            if not opt:
                return path, key
            key = opt.get("next")
        else:
            if not all(checks.get(i["key"]) for i in node["items"]):
                return path, key
            key = node.get("next")
    return path, None


def _section_done(sec: dict, state: dict) -> bool:
    return _walk(sec, state.get("sections", {}).get(sec["key"], {}))[1] is None


def _needed(step: dict) -> list[dict]:
    """The sections a step waits for. A "carry_on" section (e.g. the renewal lease) doesn't hold the step
    up: it can still be worked on after the step is done, until something later requires it."""
    return [s for s in step.get("sections", []) if not s.get("carry_on")]


def _step_ready(step: dict, state: dict) -> bool:
    checks = state.get("checks", {})
    return (all(checks.get(c["key"]) for c in step.get("checklist", []))
            and all(_section_done(sec, state) for sec in _needed(step)))


def _ready_date(step: dict, state: dict) -> date:
    """A finished step's date: the latest of its checklist dates and section completion dates."""
    dates = [_check_date(state.get("checks", {}), c["key"]) for c in step.get("checklist", [])]
    dates += [parse(state.get("sections", {}).get(s["key"], {}).get("done_on")) for s in _needed(step)]
    return max(filter(None, dates), default=today())


def _sections_started(state: dict) -> bool:
    return any(sst.get("answers") or any(sst.get("checks", {}).values()) for sst in state.get("sections", {}).values())


def _closing_section(step: dict, state: dict) -> Optional[dict]:
    """The completed section that finished the step: the latest-dated one."""
    secs = state.get("sections", {})
    done = [s for s in _needed(step) if secs.get(s["key"], {}).get("done_on")]
    return max(done, key=lambda s: (secs[s["key"]]["done_on"], step["sections"].index(s)), default=None)


def section_action(case: dict, tmpl: dict, step_id: str, section: str, node: str, value: Optional[str],
                   item: Optional[str], checked: bool, when: date, user: str, track: str = "main") -> tuple[str, bool]:
    """Answer a choice or tick/untick a box in one section of the current step.
    Returns (what happened, for the log; whether the whole step completed)."""
    _require_open(case)
    states = _track(case, tmpl, track)
    cur = _current_index(states)
    idx = next((i for i, s in enumerate(states) if s["id"] == step_id), None)
    if idx is None:
        raise EngineError("Unknown step.")
    step, st = _tpl_step(tmpl, step_id, track, case["flags"]), states[idx]
    sec = next((s for s in step.get("sections", []) if s["key"] == section), None)
    if not sec:
        raise EngineError(f"Unknown section: {section}")
    # A carry_on section stays open after its step is done, until the case is signed.
    carrying = sec.get("carry_on") and st["status"] == "done" and not _signed(tmpl, states)
    if idx != cur and not carrying:
        raise EngineError("Only the current step can be changed.")
    _require_not_correcting(st)
    missing = [c["label"] for c in step.get("checklist", []) if not st.get("checks", {}).get(c["key"])]
    if missing:
        raise EngineError(f"Check off {' and '.join(missing)} first.")
    sst = st.setdefault("sections", {}).setdefault(section, {"answers": {}, "checks": {}, "done_on": None})
    if node not in _walk(sec, sst)[0]:
        raise EngineError("Answer the earlier questions first.")
    n = sec["nodes"][node]
    if n["type"] == "choice":
        opt = next((o for o in n["options"] if o["value"] == value), None)
        if not opt:
            raise EngineError("Pick one of the options.")
        sst["answers"][node] = value
        # Changing an answer drops whatever was recorded down the old branch.
        path = _walk(sec, sst)[0]
        sst["answers"] = {k: v for k, v in sst["answers"].items() if k in path}
        live = {i["key"] for k in path if sec["nodes"][k]["type"] == "checks" for i in sec["nodes"][k]["items"]}
        sst["checks"] = {k: v for k, v in sst["checks"].items() if k in live}
        sst["last"] = {"node": node}
        text = f"{sec['label']}: answered \"{n['label']}\" {opt['label']}"
    else:
        it = next((i for i in n["items"] if i["key"] == item), None)
        if not it:
            raise EngineError(f"Unknown item: {item}")
        if n.get("ordered"):  # e.g. a lease has to be received before it can be satisfactory
            pos = n["items"].index(it)
            if checked and pos > 0 and not sst["checks"].get(n["items"][pos - 1]["key"]):
                raise EngineError(f"Check off {n['items'][pos - 1]['label']} first.")
            if not checked and any(sst["checks"].get(i["key"]) for i in n["items"][pos + 1:]):
                raise EngineError(f"Uncheck {n['items'][pos + 1]['label']} first.")
        sst["checks"][item] = bool(checked)
        sst["last"] = {"node": node, "item": item}
        text = f"{sec['label']}: {'checked' if checked else 'unchecked'} \"{it['label']}\""
    if _walk(sec, sst)[1] is not None:
        sst["done_on"] = None
    elif not sst.get("done_on"):
        if when > today():
            raise EngineError("The date can't be in the future.")
        sent = max(filter(None, [_check_date(st.get("checks", {}), c["key"]) for c in step.get("checklist", [])]), default=None)
        floor = max(filter(None, [_clock_start(case, states, idx, track, tmpl), sent]))
        if when < floor:
            raise EngineError(f"That's before {step['checklist'][0]['label'] if sent == floor else 'the previous step was completed'} ({nice(floor)}).")
        sst["done_on"] = iso(when)
        text += f", completing {sec['label']} ({nice(when)})"
    if idx == cur and _step_ready(step, st):
        complete_step(case, tmpl, step_id, _ready_date(step, st), user, track)
        return text, True
    return text, False


def _signed(tmpl: dict, states: list[dict]) -> bool:
    ids = {s["id"] for s in tmpl["steps"] if s.get("milestone") == "signing"}
    return any(s["id"] in ids and s["status"] == "done" for s in states)


# ---- corrections: sending the case back to an earlier step to fix its documents ------------------
# A step's template can have "correction": {"label", "back_to", "after", "before"}: once its "after"
# box is checked and while its "before" box isn't, the case can be sent back to the "back_to" step.
# That step reopens in correction mode, where each document (on nodes marked "issues") gets an Issue
# box. Once at least one issue has been flagged and every issue is cleared again, the earlier step is
# done again and the case returns to the step that sent it back, starting that step over.

def _require_not_correcting(st: dict) -> None:
    if st.get("correction"):
        raise EngineError("This step is being corrected. Use the Issue boxes next to the documents.")


def request_correction(case: dict, tmpl: dict, step_id: str, when: date, user: str, track: str = "main") -> str:
    """Send the case back from the current step. Returns the label of the step it went back to."""
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    if idx is None or states[idx]["id"] != step_id:
        raise EngineError("Only the current step can send the case back.")
    step, st = _tpl_step(tmpl, step_id, track, case["flags"]), states[idx]
    corr = step.get("correction")
    if not corr:
        raise EngineError("This step can't send the case back.")
    checks = st.get("checks", {})
    labels = {c["key"]: c["label"] for c in step.get("checklist", [])}
    if not checks.get(corr["after"]):
        raise EngineError(f"Check off {labels[corr['after']]} first.")
    if checks.get(corr["before"]):
        raise EngineError(f"{labels[corr['before']]} is already checked.")
    if when > today():
        raise EngineError("The date can't be in the future.")
    start = _check_date(checks, corr["after"])
    if start and when < start:
        raise EngineError(f"That's before {labels[corr['after']]} ({nice(start)}).")
    back = next((s for s in states if s["id"] == corr["back_to"]), None)
    if not back or back["status"] != "done":
        raise EngineError("The step to go back to isn't finished.")
    st.setdefault("corrections", []).append({"started": iso(when), "resolved": None, "checks": dict(checks)})
    st["checks"] = {}
    back["correction"] = {"started": iso(when), "from": step_id, "prev_date": back["date"], "prev_by": back["by"],
                          "issues": {}, "seen": False, "last": None}
    back.update(status="pending", date=None, by=None)
    return back["label"]


def set_issue(case: dict, tmpl: dict, step_id: str, section: str, item: str, checked: bool, when: date,
              user: str, track: str = "main") -> tuple[str, bool]:
    """Flag or clear an issue on a document of a step being corrected.
    Returns (what happened, for the log; whether every issue is now cleared and the case moved back on)."""
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    st = states[idx] if idx is not None else None
    if not st or st["id"] != step_id or not st.get("correction"):
        raise EngineError("Issues can only be marked while this step is being corrected.")
    step = _tpl_step(tmpl, step_id, track)
    sec = next((s for s in step.get("sections", []) if s["key"] == section), None)
    path = _walk(sec, st.get("sections", {}).get(section, {}))[0] if sec else []
    doc = next((i for k in path if sec["nodes"][k].get("issues") for i in sec["nodes"][k]["items"] if i["key"] == item), None)
    if not doc:
        raise EngineError("Unknown document.")
    corr = st["correction"]
    corr["issues"][f"{section}:{item}"] = bool(checked)
    corr["last"] = f"{section}:{item}"
    if checked:
        corr["seen"] = True
        return f"flagged an issue with \"{doc['label']}\"", False
    text = f"cleared the issue with \"{doc['label']}\""
    if not corr["seen"] or any(corr["issues"].values()):
        return text, False
    if when > today():
        raise EngineError("The date can't be in the future.")
    if when < parse(corr["started"]):
        raise EngineError(f"That's before the correction started ({nice(parse(corr['started']))}).")
    st.pop("correction")
    st.setdefault("correction_log", []).append({**corr, "resolved": iso(when)})
    st.update(status="done", date=iso(when), by=user)
    frm = next(s for s in states if s["id"] == corr["from"])
    frm["corrections"][-1]["resolved"] = iso(when)
    _recompute_status(case)
    return text, True


def _undo_correction(states: list[dict]) -> Optional[dict]:
    """Undo around a correction, if the last thing that happened was one. Returns what was undone."""
    open_ = next((s for s in states if s.get("correction")), None)
    if open_:  # cancel the open correction: the earlier step is done again and nothing was lost
        corr = open_.pop("correction")
        open_.update(status="done", date=corr["prev_date"], by=corr["prev_by"])
        frm = next(s for s in states if s["id"] == corr["from"])
        frm["checks"] = frm["corrections"].pop()["checks"]
        return {**open_, "label": f"{open_['label']} correction"}
    last_done = next((s for s in reversed(states) if s["status"] == "done"), None)
    log = (last_done or {}).get("correction_log") or []
    if log and log[-1]["resolved"] == last_done["date"]:  # reopen the correction that just finished
        corr = {k: v for k, v in log.pop().items() if k != "resolved"}
        corr["issues"][corr["last"]] = True
        was = dict(last_done)
        last_done["correction"] = corr
        last_done.update(status="pending", date=None, by=None)
        frm = next(s for s in states if s["id"] == corr["from"])
        frm["corrections"][-1]["resolved"] = None
        frm["checks"] = {}
        return was
    return None


# ---- scheduling: a step that books the next one (e.g. the review period books the signing) --------------
# A step with "schedules": "<step id>" asks for that step's date and time, which can't be before its own
# review period is over. Once the review is over and a time is booked, the step finishes by itself on the
# day the review ended. The booked step (with "scheduled_question") shows the booking, and once that time
# has passed asks whether it happened: yes finishes it on the booked date, no clears the booking to rebook.

def _at(sched: dict) -> datetime:
    return datetime.fromisoformat(sched["at"])


def _booking_pair(tpl_steps: list[dict], states: list[dict], idx: int) -> Optional[tuple[int, int]]:
    """For the step at idx: (index of the step that books, index of the booked step), or None."""
    tpl = {s["id"]: s for s in tpl_steps}
    ids = [s["id"] for s in states]
    step = tpl.get(ids[idx], {})
    if step.get("self_schedule"):  # e.g. the interview: booked from its own step
        return idx, idx
    if step.get("schedules") in ids:
        return idx, ids.index(step["schedules"])
    if step.get("scheduled_question"):
        by = next((i for i, sid in enumerate(ids) if tpl.get(sid, {}).get("schedules") == ids[idx]), None)
        if by is not None:
            return by, idx
    return None


def _held_item(step: dict) -> Optional[dict]:
    """A booked step's box that's ticked by answering that it happened (e.g. Interview held); the rest of
    its checklist is worked on after that."""
    return next((c for c in step.get("checklist", []) if c.get("from_schedule")), None)


def _tpl_steps(tmpl: dict, track: str) -> list[dict]:
    return (tmpl.get("side_track") or {}).get("steps", []) if track != "main" else tmpl["steps"]


def schedule_step(case: dict, tmpl: dict, step_id: str, on: date, at: str, user: str,
                  track: str = "main") -> tuple[str, datetime]:
    """Book (or rebook) the time from the current step. Returns (what was booked, e.g. 'Signing'; when)."""
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    if idx is None or states[idx]["id"] != step_id:
        raise EngineError("Only the current step can be scheduled.")
    pair = _booking_pair(_tpl_steps(tmpl, track), states, idx)
    if not pair:
        raise EngineError("This step has nothing to schedule.")
    by_i, target_i = pair
    by_step = _tpl_step(tmpl, states[by_i]["id"], track)
    label = by_step.get("schedule_label", states[target_i]["label"])
    if (held := _held_item(by_step)) and states[target_i].get("checks", {}).get(held["key"]):
        raise EngineError(f"The {label.lower()} already happened. Uncheck {held['label']} to rebook it.")
    try:
        clock = datetime.strptime(at or "", "%H:%M").time()
    except ValueError:
        raise EngineError("Pick a time.")
    if blocked := _schedule_blocked(case, tmpl, by_step, states):
        raise EngineError(blocked)
    end, why = _not_before(case, tmpl, by_step, states, by_i)
    if end and on < end:
        raise EngineError(f"{why} isn't over until {nice(end)}, so the {label.lower()} can't be scheduled on this date.")
    when = datetime.combine(on, clock)
    states[target_i]["scheduled"] = {"at": when.isoformat(timespec="minutes"), "on": iso(today()), "by": user}
    return label, when


def answer_scheduled(case: dict, tmpl: dict, step_id: str, happened: bool, user: str, track: str = "main") -> datetime:
    """Say whether the booked step happened. Returns the time it was booked for."""
    _require_open(case)
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    st = states[idx] if idx is not None else None
    step = _tpl_step(tmpl, step_id, track, case["flags"]) if st else {}
    if not st or st["id"] != step_id or not step.get("scheduled_question"):
        raise EngineError("Only the current step can be confirmed.")
    if not st.get("scheduled"):
        raise EngineError("Nothing is scheduled yet.")
    when = _at(st["scheduled"])
    if when > datetime.now():
        raise EngineError(f"It's scheduled for {nice_at(when)}, which hasn't come yet.")
    held = _held_item(step)
    if happened and held:  # tick the box; the rest of the checklist comes after
        st.setdefault("checks", {})[held["key"]] = iso(when.date())
        if _step_ready(step, st):
            complete_step(case, tmpl, step_id, _ready_date(step, st), user, track)
    elif happened:
        complete_step(case, tmpl, step_id, when.date(), user, track)
    else:
        st.pop("scheduled")
    return when


def _not_before(case: dict, tmpl: dict, by_step: dict, states: list[dict], by_i: int) -> tuple[Optional[date], str]:
    """The first day the booking step can book for, and what sets it: its own review period, or the review
    of an earlier step named in "schedule_requires" (e.g. 14 days after the FDD & NDA came back)."""
    ends = []
    if end := _review_end(case, by_step, states, by_i):
        ends.append((end, f"The {by_step['review_days']}-day review period"))
    for sid in by_step.get("schedule_requires", {}).get("reviews", []):
        i = next((j for j, s in enumerate(states) if s["id"] == sid), None)
        other = _tpl_step(tmpl, sid, "main")
        if i is not None and (end := _review_end(case, other, states, i)):
            ends.append((end, f"The {other['review_days']}-day {other['label']} review period"))
    return max(ends, default=(None, ""))


def _schedule_blocked(case: dict, tmpl: dict, by_step: dict, states: list[dict]) -> Optional[str]:
    """Why the booking can't be made yet at all, e.g. the lease hasn't been confirmed, or the side track
    ("side_track" in "schedule_requires", e.g. the renewal inspection) isn't finished."""
    what = by_step.get("schedule_label", "date").lower()
    for need in by_step.get("schedule_requires", {}).get("sections", []):
        i = next((j for j, s in enumerate(states) if s["id"] == need["step"]), None)
        sec = next((x for x in _tpl_step(tmpl, need["step"], "main").get("sections", []) if x["key"] == need["section"]), None)
        if i is not None and sec and not _section_done(sec, states[i]):
            return f"The {need['label']} has to be done ({states[i]['label']}) before the {what} can be scheduled."
    if (need := by_step.get("schedule_requires", {}).get("side_track")) and not _all_finished(case.get("side_steps", [])):
        return f"The {need} has to be done before the {what} can be scheduled."
    return None


def _finish_booked_review(case: dict, tmpl: dict) -> None:
    """A step that books the next one finishes by itself once its review is over and the time is booked."""
    states = case["steps"]
    idx = _current_index(states)
    if case["status"] != "active" or idx is None:
        return
    step = _tpl_step(tmpl, states[idx]["id"], "main")
    target = next((s for s in states if s["id"] == step.get("schedules")), None)
    sched = (target or {}).get("scheduled")
    if not sched:
        return
    end = _review_end(case, step, states, idx) or parse(sched["on"])
    if end <= today():
        states[idx].update(status="done", date=iso(end), by=sched["by"], reason=None)


# ---- reminders: follow-ups due a set time before one of the case's dates --------------------------------
# A step with "reminders": {"due": "<date key>", "what": "...", "items": [{"key", "label", "before_months" or
# "before_days"}]} shows each reminder from that time on, until it's ticked as done or the step is finished.

def _reminder_from(due: date, r: dict) -> date:
    return add_months(due, -r["before_months"]) if r.get("before_months") else date.fromordinal(due.toordinal() - r.get("before_days", 0))


def set_reminder(case: dict, tmpl: dict, step_id: str, key: str, done: bool, when: date, user: str) -> str:
    """Tick (or untick) a reminder on the current step as followed up. Returns its label."""
    _require_open(case)
    idx = _current_index(case["steps"])
    if idx is None or case["steps"][idx]["id"] != step_id:
        raise EngineError("Only the current step's reminders can be changed.")
    rem = _tpl_step(tmpl, step_id, "main").get("reminders") or {}
    item = next((r for r in rem.get("items", []) if r["key"] == key), None)
    if not item:
        raise EngineError(f"Unknown reminder: {key}")
    if when > today():
        raise EngineError("The date can't be in the future.")
    case["steps"][idx].setdefault("reminders", {})[key] = iso(when) if done else None
    return item["label"]


def _reminders_view(case: dict, step: dict, st: dict) -> Optional[dict]:
    rem = step.get("reminders")
    if not rem:
        return None
    due = parse(case.get("dates", {}).get(rem["due"]))
    items = [{"key": r["key"], "label": r["label"], "from": iso(_reminder_from(due, r)) if due else None,
              "done": st.get("reminders", {}).get(r["key"])} for r in rem["items"]]
    return {"what": rem["what"], "due": iso(due), "items": items}


def skip_step(case: dict, tmpl: dict, step_id: str, reason: str, user: str, track: str = "main") -> dict:
    _require_open(case)
    if not (reason or "").strip():
        raise EngineError("Add a reason for skipping this step.")
    states = _track(case, tmpl, track)
    idx = _current_index(states)
    if idx is None or states[idx]["id"] != step_id:
        raise EngineError("Only the current step can be skipped.")
    _require_not_correcting(states[idx])
    states[idx].update(status="skipped", auto=False, date=iso(today()), by=user, reason=reason.strip())
    _recompute_status(case)
    return states[idx]


def undo_last(case: dict, tmpl: dict, user: str, track: str = "main") -> dict:
    if case["status"] not in ("active", "completed"):
        raise EngineError("Reopen this case before undoing steps.")
    states = _track(case, tmpl, track, started=False)
    if was := _undo_correction(states):
        _recompute_status(case)
        return was
    side = tmpl.get("side_track") or {}
    ids = [s["id"] for s in states]
    gate = ids.index(side["starts_after"]) if track == "main" and side.get("starts_after") in ids else -1
    for i in range(len(states) - 1, -1, -1):
        if i == 0 and track == "main" and not _tpl_step(tmpl, states[0]["id"], track).get("checklist"):
            break  # the start of the case can't be undone; delete/close the case instead
        s = states[i]
        if s["status"] == "done" or (s["status"] == "skipped" and not s.get("auto")):
            if i <= gate and _side_started(case):  # the side track started off this step
                raise EngineError(f"Undo the {side['label']} first: it started once this step was done.")
            was = dict(s)
            s.update(status="pending", date=None, by=None, reason=None, auto=False)
            step = _tpl_step(tmpl, s["id"], track, case["flags"])
            if booked := next((x for x in states if x["id"] == step.get("schedules")), None):
                booked.pop("scheduled", None)  # otherwise the step would finish again by itself
            sec = _closing_section(step, s)
            if sec:  # reopen the section that finished the step by reversing its last answer or box
                sst = s["sections"][sec["key"]]
                last = sst.get("last") or {}
                if last.get("item"):
                    sst["checks"][last["item"]] = False
                else:
                    sst["answers"].pop(last.get("node"), None)
                sst["done_on"] = None
            elif closing := _closing_item(step.get("checklist", []), s.get("checks", {})):
                s["checks"][closing["key"]] = None  # reopen by unticking the box that finished the step
            _recompute_status(case)
            return was
    raise EngineError("There's nothing to undo.")


def set_step_date(case: dict, tmpl: dict, step_id: str, new_date: date, track: str = "main") -> dict:
    _require_open(case)
    states = _track(case, tmpl, track, started=False)
    idx = next((i for i, s in enumerate(states) if s["id"] == step_id), None)
    if idx is None or states[idx]["status"] != "done":
        raise EngineError("Only completed steps have a date to change.")
    if new_date > today():
        raise EngineError("The date can't be in the future.")
    before = [parse(s["date"]) for s in states[:idx] if s["status"] == "done" and s["date"]]
    after = [parse(s["date"]) for s in states[idx + 1:] if s["status"] == "done" and s["date"]]
    if before and new_date < max(before):
        raise EngineError(f"That's before the previous step ({nice(max(before))}).")
    if after and new_date > min(after):
        raise EngineError(f"That's after the next completed step ({nice(min(after))}).")
    step = _tpl_step(tmpl, step_id, track, case["flags"])
    items = step.get("checklist", [])
    checks = states[idx].get("checks", {})
    closing = None if step.get("sections") else _closing_item(items, checks)
    if sec := _closing_section(step, states[idx]):  # the step's date is when its last section finished
        secs = states[idx]["sections"]
        others = [parse(secs.get(s["key"], {}).get("done_on")) for s in _needed(step) if s is not sec]
        others += [_check_date(checks, c["key"]) for c in items]
        latest = max(filter(None, others), default=None)
        if latest and new_date < latest:
            raise EngineError(f"That's before other parts of this step were finished ({nice(latest)}).")
        secs[sec["key"]]["done_on"] = iso(new_date)
    if closing:  # the step's date is the date its last box was checked, so move that box's date too
        others = [(c, _check_date(checks, c["key"])) for c in items if c is not closing]
        latest = max(((c, d) for c, d in others if d), key=lambda x: x[1], default=None)
        if latest and new_date < latest[1]:
            raise EngineError(f"That's before {_item_name(step, latest[0])} ({nice(latest[1])}).")
        checks[closing["key"]] = iso(new_date)
    if idx == 0 and track == "main":
        case["start_date"] = iso(new_date)
    states[idx]["date"] = iso(new_date)
    return states[idx]


def update_case(case: dict, tmpl: dict, fields: dict) -> list[str]:
    """Apply edits to case details. Returns the names of the fields that changed."""
    changed = []
    for key in ("store_name", "franchisee", "transferee", "related_units", "box_url"):
        if key in fields and fields[key] is not None:
            val = fields[key].strip()
            if key == "store_name" and not val:
                raise EngineError("Store name can't be empty.")
            if case.get(key) != val:
                case[key], _ = val, changed.append(key)
    if fields.get("units") is not None:
        val = _units(fields["units"])
        if case.get("units", []) != val:
            case["units"], _ = val, changed.append("related_units")
    if fields.get("dates") is not None or fields.get("flags"):  # a flag can make a date required
        before = case.get("dates", {})
        case["dates"] = _dates(tmpl, fields.get("dates") or {}, before, {**case["flags"], **(fields.get("flags") or {})}, case["flags"])
        changed += [d["key"] for d in tmpl.get("dates", []) if before.get(d["key"]) != case["dates"].get(d["key"])]
    for key in ("target_signing_date", "lease_deadline"):
        if key in fields:
            val = iso(fields[key])
            if case.get(key) != val:
                case[key], _ = val, changed.append(key)
    if fields.get("start_date"):
        new_start: date = fields["start_date"]
        others = [parse(s["date"]) for s in case["steps"][1:] if s["status"] == "done" and s["date"]]
        if new_start > today():
            raise EngineError("The start date can't be in the future.")
        if others and new_start > min(others):
            raise EngineError(f"The start date can't be after later steps ({nice(min(others))}).")
        if case["start_date"] != iso(new_start):
            case["start_date"] = iso(new_start)
            if case["steps"][0]["status"] == "done":
                case["steps"][0]["date"] = iso(new_start)
            changed.append("start_date")
    if fields.get("flags") is not None:
        valid = {f["key"] for f in tmpl.get("flags", [])}
        for k, v in fields["flags"].items():
            if k not in valid:
                raise EngineError(f"Unknown option: {k}")
            if bool(case["flags"].get(k)) != bool(v):
                case["flags"][k] = bool(v)
                changed.append(k)
        sync(case, tmpl)
        # Dropping boxes (e.g. no background check for an existing franchisee) can leave the current step done.
        idx = _current_index(case["steps"])
        if idx is not None and case["status"] == "active":
            step = _tpl_step(tmpl, case["steps"][idx]["id"], "main", case["flags"])
            if step.get("checklist") and _step_ready(step, case["steps"][idx]):
                done_on = case["start_date"] if idx == 0 else iso(_ready_date(step, case["steps"][idx]))
                case["steps"][idx].update(status="done", date=done_on, by=None, reason=None)
                _recompute_status(case)
    return changed


def add_note(case: dict, text: str, follow_up: Optional[date], user: str, tmpl: Optional[dict] = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise EngineError("The note is empty.")
    # Remember where the case was when the note was written, e.g. "Contracts Team Evaluation".
    idx, side = _current_index(case["steps"]), _current_index(case.get("side_steps", []))
    track = ((tmpl or {}).get("side_track") or {}).get("label", "Lease")
    step = (case["steps"][idx]["label"] if idx is not None
            else f"{track}: {case['side_steps'][side]['label']}" if side is not None else None)
    note = {"id": uuid.uuid4().hex[:8], "text": text, "author": user, "created_at": _now(), "step": step,
            "follow_up_date": iso(follow_up), "resolved": False, "resolved_by": None, "resolved_at": None}
    case["notes"].append(note)
    return note


def resolve_note(case: dict, note_id: str, user: str) -> dict:
    note = next((n for n in case["notes"] if n["id"] == note_id), None)
    if not note:
        raise EngineError("Note not found.")
    note.update(resolved=True, resolved_by=user, resolved_at=_now())
    return note


def close_case(case: dict, outcome: str, reason: str) -> None:
    if case["status"] != "active":
        raise EngineError(f"This case is already {case['status'].replace('_', ' ')}.")
    if not (reason or "").strip():
        raise EngineError("Add a short reason so the history makes sense later.")
    if case.get("hold"):
        resume(case, max(today(), parse(case["hold"]["start"])), "closed")
    case["status"], case["closed_reason"] = outcome, reason.strip()


# ---- holds: a case paused on something outside the Contracts Team ---------------------------------
# A hold is set on the whole case and remembers the step it was set at. While it's on, the clock on the
# current step stops, but boxes can still be ticked. Every hold stays on record with its reason.
# A template step with "only_if_held_at": "<step id>" (e.g. Management Confirmation) only appears once the
# case has been put on hold at that step.

def put_on_hold(case: dict, tmpl: dict, reason: str, when: date, user: str) -> str:
    """Put the case on hold at its current step. Returns that step's label."""
    _require_open(case)
    if case.get("hold"):
        raise EngineError("This case is already on hold.")
    if not (reason or "").strip():
        raise EngineError("Add the reason it's on hold.")
    if when > today():
        raise EngineError("The date can't be in the future.")
    idx = _current_index(case["steps"])
    st = case["steps"][idx] if idx is not None else None
    label = st["label"] if st else _waiting_label(tmpl)
    floor = _entered(case, case["steps"], idx) if st else parse(case["start_date"])
    last_end = max((parse(h["end"]) for h in case.get("holds", [])), default=None)
    floor = max(filter(None, [floor, last_end]))
    if when < floor:
        raise EngineError(f"That's before {label} started ({nice(floor)}).")
    case["hold"] = {"step_id": st["id"] if st else None, "step_label": label, "start": iso(when),
                    "reason": reason.strip(), "by": user}
    sync(case, tmpl)  # a hold can bring in a step, e.g. Management Confirmation
    return label


def resume(case: dict, when: date, user: str) -> dict:
    """Take the case off hold. Returns the finished hold."""
    hold = case.get("hold")
    if not hold:
        raise EngineError("This case isn't on hold.")
    if when > today():
        raise EngineError("The date can't be in the future.")
    if when < parse(hold["start"]):
        raise EngineError(f"That's before it was put on hold ({nice(parse(hold['start']))}).")
    done = {**hold, "end": iso(when), "resumed_by": user}
    case.setdefault("holds", []).append(done)
    case["hold"] = None
    return done


def _holds_view(case: dict, t: date) -> list[dict]:
    return [{"step_id": h.get("step_id"), "step_label": h["step_label"], "start": h["start"], "end": h.get("end"),
             "days": max(((parse(h["end"]) if h.get("end") else t) - parse(h["start"])).days, 0),
             "reason": h["reason"], "by": h["by"], "resumed_by": h.get("resumed_by")} for h in _all_holds(case)]


def reopen_case(case: dict) -> None:
    if case["status"] in ("not_approved", "withdrawn"):
        case["status"], case["closed_reason"] = "active", None
        _recompute_status(case)
    else:
        raise EngineError("Only closed cases can be reopened. Use Undo on a completed case.")


# ---- views: everything the UI needs, computed for a given day -----------------------

def _hints(step: dict, flags: dict) -> list[str]:
    out = [step["hint"]] if step.get("hint") else []
    for h in step.get("hints", []):
        if bool(flags.get(h["when"]["flag"], False)) == h["when"]["equals"]:
            out.append(h["text"])
    return out


def _checklist_note(v: dict) -> Optional[str]:
    """Once a checklist is underway, a one-line status for lists, e.g. 'Waiting on confirmation to proceed'
    or 'Accounting: requested · Operations: not requested'."""
    if corr := v.get("correction"):
        n = corr["open_issues"]
        return (f"{v['label']} correction: {n} open issue{'s' if n != 1 else ''}" if corr["seen"]
                else f"{v['label']} correction: mark the documents with issues")
    cl = v["checklist"]
    if not any(c["checked"] for c in cl):
        return None
    lower = lambda s: s if s.split()[0].isupper() else s[0].lower() + s[1:]  # keep acronyms like FDD
    if v["sections"] and all(c["checked"] for c in cl):
        open_ = [s.get("waiting") or lower(s["label"]) for s in v["sections"] if not s["done"]]
        return f"Waiting on {', '.join(open_)}" if open_ else None
    if not v["groups"]:
        nxt = next((c for c in cl if not c["checked"]), None)
        if nxt and nxt.get("status"):  # a box can set the whole line, e.g. "Legal drafting Franchise Agreement"
            return nxt["status"]
        return f"Waiting on {nxt.get('waiting') or lower(nxt['label'])}" if nxt else None
    parts = []
    for g in v["groups"]:
        items = [c for c in cl if c["group"] == g["key"]]
        done = [c for c in items if c["checked"]]
        parts.append(f"{g.get('short', g['label'])}: {lower(done[-1]['label']) if done else 'not ' + lower(items[0]['label'])}")
    return " · ".join(parts)


def _team(step: dict, st: dict) -> str:
    """Who the current step is waiting on. A box can name its own team ("owner"), which is responsible
    while that box is the next one to tick; with groups (e.g. the inspections) each unfinished group
    names its next box's team, e.g. Operations once Accounting is satisfactory."""
    checks, default = st.get("checks", {}), step.get("owner", "")
    teams = []
    for c in step.get("checklist", []):
        group = _group_of(step["checklist"], c)
        if next((x for x in group if not checks.get(x["key"])), None) is c:
            team = c.get("owner") or default
            if team not in teams:
                teams.append(team)
    return " & ".join(teams) or default


def _sections_view(step: dict, st: dict) -> list[dict]:
    out = []
    issues = (st.get("correction") or {}).get("issues", {})
    for sec in step.get("sections", []):
        sst = st.get("sections", {}).get(sec["key"], {})
        path, waiting = _walk(sec, sst)
        nodes = []
        for k in path:
            n = sec["nodes"][k]
            if n["type"] == "choice":
                nodes.append({"key": k, "type": "choice", "label": n["label"], "answer": sst.get("answers", {}).get(k),
                              "options": [{"value": o["value"], "label": o["label"]} for o in n["options"]]})
            else:
                nodes.append({"key": k, "type": "checks", "label": n.get("label"),
                              "instruction": n.get("instruction"), "ordered": bool(n.get("ordered")),
                              "issues": bool(n.get("issues")),
                              "items": [{"key": i["key"], "label": i["label"], "checked": bool(sst.get("checks", {}).get(i["key"])),
                                         "issue": bool(issues.get(f"{sec['key']}:{i['key']}"))}
                                        for i in n["items"]]})
        # A finished section can name its outcome in the template ("done_text" on the last node, or on the
        # option that ended it), e.g. "Lease on file, covers renewal term".
        done_text = None
        if waiting is None and path:
            last = sec["nodes"][path[-1]]
            if last["type"] == "choice":
                ans = sst.get("answers", {}).get(path[-1])
                done_text = next((o.get("done_text") for o in last["options"] if o["value"] == ans), None)
            else:
                done_text = last.get("done_text")
        out.append({"key": sec["key"], "label": sec["label"], "waiting": sec.get("waiting"), "done_text": done_text,
                    "done": waiting is None, "done_on": sst.get("done_on"), "nodes": nodes})
    return out


def _steps_view(case, tmpl, tpl_steps, states, t, default_stale, active, timed=True, track="main"):
    tpl = {s["id"]: s for s in tpl_steps}
    cur_idx = _current_index(states) if active else None
    out, current = [], None
    for i, st in enumerate(states):
        step = _for_flags(tpl.get(st["id"], {}), case["flags"])
        v = {
            "id": st["id"], "label": st["label"], "status": st["status"], "date": st["date"],
            "by": st["by"], "reason": st["reason"], "auto": st.get("auto", False),
            "indent": step.get("indent", 0), "owner": step.get("owner", ""),
            "hints": _hints(step, case["flags"]), "review_days": step.get("review_days"),
            "milestone": step.get("milestone"), "is_current": i == cur_idx,
            # a step that only applies after a hold (e.g. Management Confirmation) stays out of sight until then
            "hidden": bool(step.get("only_if_held_at")) and st["status"] == "skipped" and st.get("auto", False),
            "holds": [h for h in _holds_view(case, t) if h["step_id"] == st["id"]],
            "checklist": [{"key": c["key"], "label": c["label"], "group": c.get("group"), "waiting": c.get("waiting"),
                           "status": c.get("status"), "show_after_previous": bool(c.get("show_after_previous")),
                           "hide_when_done": bool(c.get("hide_when_done")), "from_schedule": bool(c.get("from_schedule")), "after": c.get("after"),
                           "checked": bool(st.get("checks", {}).get(c["key"])),
                           "date": iso(_check_date(st.get("checks", {}), c["key"]))}
                          for c in step.get("checklist", [])],
            "groups": step.get("groups", []),
            "sections": _sections_view(step, st),
            "corrections": [{"started": c["started"], "resolved": c["resolved"]} for c in st.get("corrections", [])],
        }
        if corr := st.get("correction"):
            v["correction"] = {"started": corr["started"], "seen": corr["seen"],
                               "open_issues": sum(1 for x in corr["issues"].values() if x),
                               "from_label": next((s["label"] for s in states if s["id"] == corr["from"]), corr["from"])}
        if (c := step.get("correction")) and i == cur_idx:
            checks = st.get("checks", {})
            v["can_correct"] = bool(checks.get(c["after"])) and not checks.get(c["before"])
            v["correction_label"] = c["label"]
        v["status_note"] = _checklist_note(v)
        if sent_on := _started_by_date(step, states):
            v["started_by"] = {"label": step["started_by"]["label"], "date": iso(sent_on)}
        if st["status"] == "done" and (review_end := _review_end(case, step, states, i)):
            v["review_ends"] = iso(review_end)  # e.g. the FDD & NDA review still running after the step is done
            v["review_over"] = t >= review_end
        if any(sec.get("carry_on") for sec in step.get("sections", [])):
            v["carry_on_open"] = st["status"] == "done" and not _signed(tmpl, states)
        if i == cur_idx:
            entered = _entered(case, states, i, track, tmpl)
            pair = _booking_pair(tpl_steps, states, i)
            sched = states[pair[1]].get("scheduled") if pair else None
            if pair and pair[1] == i and sched:  # a booked step's clock starts at the booking
                entered = max(entered, _at(sched).date())
            rd = step.get("review_days")
            stale_after = step.get("stale_after_days") or ((rd + 3) if rd else default_stale)
            if step.get("stale_after_request_days") and (requested := _request_clock(step, st)):
                entered, stale_after = max(entered, requested), step["stale_after_request_days"]
            held = _held_days(case, entered, t)
            days = max((t - entered).days - held, 0)
            v.update(entered_at=iso(entered), days_in_step=days, days_on_hold=held, stale_after=stale_after,
                     owner=_team(step, st))
            if timed:
                v["is_stale"] = days > stale_after and not case.get("hold") and not step.get("untimed")
            if review_end := _review_end(case, step, states, i):
                v["review_ends"] = iso(review_end)
                v["review_over"] = t >= review_end
            if timed and (rem := _reminders_view(case, step, st)):
                v["reminders"] = rem
            if pair:
                by_step = tpl.get(states[pair[0]]["id"], {})
                label = by_step.get("schedule_label", states[pair[1]]["label"])
                now = datetime.now() if t == today() else datetime.combine(t, datetime.max.time())
                passed = bool(sched) and _at(sched) <= now
                held = _held_item(step)
                held = bool(held) and bool(st.get("checks", {}).get(held["key"]))
                question = step.get("scheduled_question") if pair[1] == i and not held else None
                not_before, why = _not_before(case, tmpl, by_step, states, pair[0])
                v["schedule"] = {"label": label, "at": sched["at"] if sched else None, "passed": passed,
                                 "question": question, "held": held, "not_before": iso(not_before),
                                 "not_before_why": why, "blocked": _schedule_blocked(case, tmpl, by_step, states)}
                v["status_note"] = (v["status_note"] if held
                                    else question if question and passed
                                    else f"{label} scheduled for {nice_at(_at(sched))}" if sched
                                    else f"{label} not scheduled yet")
            current = v
        out.append(v)
    return out, current


def build_view(case: dict, tmpl: dict, t: Optional[date] = None, config: Optional[dict] = None) -> dict:
    t = t or today()
    default_stale = (config or {}).get("default_stale_days", DEFAULT_STALE_DAYS)
    active = case["status"] == "active"
    steps, current = _steps_view(case, tmpl, tmpl["steps"], case["steps"], t, default_stale, active)
    side_tpl = tmpl.get("side_track")
    side = None
    if side_tpl:
        started, started_on = _side_gate(case, tmpl)
        alongside = bool(side_tpl.get("alongside"))
        s_steps, s_cur = _steps_view(case, tmpl, side_tpl["steps"], case["side_steps"], t, default_stale,
                                     active and started, timed=alongside, track="side")
        side = {"label": side_tpl["label"], "steps": s_steps, "current": s_cur,
                "done": _all_finished(case["side_steps"]), "started": started, "started_on": iso(started_on),
                "starts_note": side_tpl.get("starts_note"), "note": side_tpl.get("note"), "alongside": alongside,
                "waiting_label": _waiting_label(tmpl)}

    counted = [s for s in steps if not (s["status"] == "skipped" and s["auto"])]
    done = sum(1 for s in counted if s["status"] in ("done", "skipped"))
    progress = {"done": done, "total": len(counted), "percent": round(100 * done / len(counted)) if counted else 0}

    signing = next((parse(s["date"]) for s in steps if s["milestone"] == "signing" and s["status"] == "done"), None)
    effective = None
    if signing and tmpl.get("effective_after_signing_months"):
        effective = add_months(signing, tmpl["effective_after_signing_months"])
    lease_deadline = None
    if side and tmpl.get("lease_deadline_months") and not side["done"]:
        explicit = parse(case.get("lease_deadline"))
        auto = add_months(signing, tmpl["lease_deadline_months"]) if signing else None
        d = explicit or auto
        if d:
            lease_deadline = {"date": iso(d), "days_left": (d - t).days, "source": "set" if explicit else "auto"}

    exp_def = next((d for d in tmpl.get("dates", []) if d["key"] == tmpl.get("expiration_date")), {})
    expires = parse(case.get("dates", {}).get(exp_def["key"])) if exp_def and _applies(exp_def.get("only_when"), case["flags"]) else None
    notes = []
    for n in case["notes"]:
        n = dict(n)
        fu = parse(n["follow_up_date"])
        n["overdue"] = bool(fu and not n["resolved"] and fu < t)
        n["due_today"] = bool(fu and not n["resolved"] and fu == t)
        notes.append(n)

    attention = []
    if active:
        for cur in (current, (side or {}).get("current")):
            if cur and cur.get("is_stale"):
                attention.append({
                    "kind": "stale", "level": "alert" if cur["days_in_step"] > 2 * cur["stale_after"] else "warn",
                    "text": f"{cur['days_in_step']} days on \"{cur['label']}\" (flagged after {cur['stale_after']})"})
        for n in notes:
            if n["overdue"]:
                days = (t - parse(n["follow_up_date"])).days
                attention.append({"kind": "follow_up", "level": "alert",
                                  "text": f"Follow-up {days} day{'s' if days != 1 else ''} overdue: {n['text'][:90]}"})
            elif n["due_today"]:
                attention.append({"kind": "follow_up", "level": "warn", "text": f"Follow up today: {n['text'][:90]}"})
        if lease_deadline:
            left = lease_deadline["days_left"]
            if left < 0:
                attention.append({"kind": "lease_deadline", "level": "alert",
                                  "text": f"Lease deadline passed {-left} days ago ({nice(parse(lease_deadline['date']))})"})
            elif left <= 30:
                attention.append({"kind": "lease_deadline", "level": "warn",
                                  "text": f"Lease deadline in {left} days ({nice(parse(lease_deadline['date']))})"})
        if rem := (current or {}).get("reminders"):
            due = parse(rem["due"]) if rem["due"] else None
            if due and t > due:
                attention.append({"kind": "due_date", "level": "alert",
                                  "text": f"{rem['what']} was due {nice(due)} ({(t - due).days} days ago)"})
            elif due:
                for r in rem["items"]:
                    if not r["done"] and t >= parse(r["from"]):
                        attention.append({"kind": "reminder", "level": "warn",
                                          "text": f"{r['label']}: {rem['what']} due {nice(due)}"})
        if expires and not signing:
            booked = next((s["scheduled"]["at"] for s in case["steps"] if s.get("scheduled")
                           and _tpl_step(tmpl, s["id"], "main").get("milestone") == "signing"), None)
            warn_days = tmpl.get("expiry_warn_days")
            if t > expires:
                attention.append({"kind": "expired", "level": "alert",
                                  "text": tmpl.get("expired_text", "FA term expired {date} and it isn't signed yet")
                                  .format(date=nice(expires))})
            elif warn_days and (expires - t).days <= warn_days:
                left = (expires - t).days
                attention.append({"kind": "expiring", "level": "warn",
                                  "text": tmpl.get("expiring_text", "Expires {date}").format(
                                      date=nice(expires), days=f"{left} day{'s' if left != 1 else ''}")})
            elif booked and _at({"at": booked}).date() > expires:
                attention.append({"kind": "expired", "level": "alert",
                                  "text": f"Signing is scheduled after the FA term expires ({nice(expires)})"})
        sc = (current or {}).get("schedule")
        if current and current.get("review_over") and not current.get("is_stale"):
            todo = f"Schedule the {sc['label'].lower()}." if sc and not sc["at"] else "Ready to move on."
            attention.append({"kind": "review_over", "level": "ready",
                              "text": f"Review period ended {nice(parse(current['review_ends']))}. {todo}"})
        if sc and sc["question"] and sc["passed"]:
            attention.append({"kind": "scheduled", "level": "ready",
                              "text": f"{sc['label']} was scheduled for {nice_at(_at(sc))}. {sc['question']}"})
    attention.sort(key=lambda a: -LEVEL_RANK[a["level"]])

    return {
        "id": case["id"], "type": case["type"], "type_label": tmpl["label"],
        "store_number": case["store_number"], "store_name": case["store_name"],
        "franchisee": case["franchisee"], "transferee": case.get("transferee", ""),
        "related_units": case.get("related_units", ""), "units": case.get("units", []),
        "status": case["status"], "closed_reason": case.get("closed_reason"),
        "completed_at": case.get("completed_at"), "start_date": case["start_date"],
        "target_signing_date": case.get("target_signing_date"), "box_url": case.get("box_url", ""),
        "dates": case.get("dates", {}), "date_defs": tmpl.get("dates", []),
        "hold": next((h for h in _holds_view(case, t) if not h["end"]), None) if case.get("hold") else None,
        "holds": _holds_view(case, t),
        "flags": case["flags"], "flag_defs": tmpl.get("flags", []),
        "start_label": start_label(tmpl, case["flags"]), "template_notes": tmpl.get("notes", []),
        "created_by": case["created_by"], "created_at": case["created_at"], "updated_at": case["updated_at"],
        "steps": steps, "current": current, "side_track": side, "progress": progress,
        "signing_date": iso(signing), "effective_date": iso(effective), "lease_deadline": lease_deadline,
        "lease_deadline_set": case.get("lease_deadline"),
        "has_signing": any(s.get("milestone") == "signing" for s in tmpl["steps"]),
        "expires": iso(expires), "links": case.get("links", []), "links_to": tmpl.get("links_to", []),
        "notes": notes, "attention": attention,
    }


def _step_summary(cur: Optional[dict]) -> Optional[dict]:
    return ({"id": cur["id"], "label": cur["label"], "owner": cur["owner"],
             "days_in_step": cur["days_in_step"], "days_on_hold": cur.get("days_on_hold", 0),
             "is_stale": cur.get("is_stale", False), "status_note": cur["status_note"]} if cur else None)


def summarize(view: dict) -> dict:
    side = view["side_track"]
    return {
        "id": view["id"], "type": view["type"], "type_label": view["type_label"],
        "store_number": view["store_number"], "store_name": view["store_name"],
        "franchisee": view["franchisee"], "transferee": view["transferee"],
        "status": view["status"], "start_date": view["start_date"],
        "target_signing_date": view["target_signing_date"], "signing_date": view["signing_date"],
        "progress": view["progress"], "updated_at": view["updated_at"], "created_by": view["created_by"],
        "current": _step_summary(view["current"]),
        "side": ({"label": side["label"], "done": side["done"], "alongside": side["alongside"],
                  "waiting_label": side["waiting_label"], "current": _step_summary(side["current"])} if side else None),
        "has_signing": view["has_signing"], "expires": view["expires"], "links": view["links"],
        "links_to": view["links_to"],
        "attention": view["attention"], "hold": view["hold"], "dates": view["dates"], "flags": view["flags"],
    }


# ---- links: a lease case tied to the renewal, transfer or expansion for the same premises ---------------
# A template with "links_to" (the lease) keeps the IDs of the cases it's linked to in the case's "links";
# both sides show the link. An open case of the other kind for the same store # that isn't linked yet is
# suggested on both sides (store # 0, a new location without a number, never matches).

def _linkable(a: dict, b: dict) -> bool:
    """Whether summary `a` (a linking case, e.g. a lease) can link to summary `b`."""
    return b["type"] in a.get("links_to", []) and a["id"] != b["id"]


def _related_row(s: dict) -> dict:
    return {"id": s["id"], "type": s["type"], "type_label": s["type_label"], "store_number": s["store_number"],
            "store_name": s["store_name"], "status": s["status"], "current": s["current"],
            "side": s["side"], "expires": s["expires"], "created_by": s["created_by"]}


def relations(me: dict, others: list[dict]) -> dict:
    """The cases linked to summary `me`, and open ones for the same store that aren't linked yet."""
    linked, suggested = [], []
    for o in others:
        if o["id"] == me["id"]:
            continue
        if (_linkable(me, o) and o["id"] in me["links"]) or (_linkable(o, me) and me["id"] in o["links"]):
            linked.append(_related_row(o))
        elif ((_linkable(me, o) or _linkable(o, me)) and o["status"] == "active" and me["status"] == "active"
              and o["store_number"].lstrip("0") and o["store_number"].lstrip("0") == me["store_number"].lstrip("0")):
            suggested.append(_related_row(o))
    return {"linked": linked, "suggested": suggested}


def add_relations(view: dict, rel: dict) -> dict:
    """Put relations() on a view or summary; an unlinked match for the same store needs attention."""
    view["related"] = rel
    if view["status"] == "active":
        for o in rel["suggested"]:
            view["attention"].append({"kind": "link", "level": "warn",
                                      "text": f"Store #{o['store_number']} also has an open {o['type_label'].lower()} "
                                              f"case ({o['id']}). Link them so both sides stay in the loop."})
        view["attention"].sort(key=lambda a: -LEVEL_RANK[a["level"]])
    return view


def set_link(case: dict, tmpl: dict, other: dict, linked: bool) -> None:
    """Link (or unlink) `other` to this case, which has to be a linking type (e.g. a lease)."""
    if other["type"] not in tmpl.get("links_to", []):
        raise EngineError(f"A {tmpl['label'].lower()} case can't be linked to a {other['type']} case.")
    links = case.setdefault("links", [])
    if linked and other["id"] in links:
        raise EngineError(f"{other['id']} is already linked.")
    if not linked and other["id"] not in links:
        raise EngineError(f"{other['id']} isn't linked.")
    case["links"] = links + [other["id"]] if linked else [x for x in links if x != other["id"]]
