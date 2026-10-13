"""Optional: load a few made-up cases so you can click around.  python -m backend.seed_demo"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from . import engine, storage


def _d(days_ago: int) -> date:
    return date.today() - timedelta(days=days_ago)


def _finish(case, tmpl, when: date, track: str = "main") -> None:
    """Finish the current step on `when` the way a person would: tick its boxes, else mark it complete.
    Steps a script can't fill in (choices, bookings) are skipped so the demo still moves along."""
    states = case["side_steps"] if track == "lease" else case["steps"]
    st = states[engine._current_index(states)]
    step = engine._tpl_step(tmpl, st["id"], track, case["flags"])
    try:
        for item in step.get("checklist", []):
            if st["status"] != "done":
                engine.set_check(case, tmpl, st["id"], item["key"], True, when, "demo", track)
        if st["status"] != "done":
            engine.complete_step(case, tmpl, st["id"], when, "demo", track)
    except engine.EngineError:
        engine.skip_step(case, tmpl, st["id"], "Demo data", "demo", track)
        st["date"] = engine.iso(when)


def _make(type_, store, name, franchisee, started, done_steps, last_gap, flags=None, transferee=""):
    """Create a case that began `started` days ago, with `done_steps` more steps done, the last `last_gap` days ago."""
    tmpl = engine.TEMPLATES[type_]
    cid = storage.next_case_id(tmpl["id_prefix"], store)
    case = engine.new_case(tmpl, cid, {
        "store_number": store, "store_name": name, "franchisee": franchisee, "transferee": transferee,
        "start_date": _d(started), "flags": flags or {},
        "dates": {"fa_expiration": _d(started - 180), "election_due": _d(started - 90)} if type_ == "renewal" else {}}, "demo")
    span = max(started - last_gap, 0)
    for i in range(done_steps):
        _finish(case, tmpl, _d(started - round(span * (i + 1) / done_steps)))
    storage.save_case(case)
    return case, tmpl


def main() -> None:
    storage.init()
    if storage.list_cases():
        sys.exit("There are already cases in data/cases, so I won't add demo data.")
    c, t = _make("renewal", "101", "Demo Store Alpha", "A. Example", 50, 5, 12)
    c, t = _make("transfer", "102", "Demo Store Bravo", "B. Example", 30, 9, 2, {"transferee_is_existing": True}, "C. Sample")
    engine.add_note(c, "Waiting on landlord contact info", _d(1), "demo")
    storage.save_case(c)
    c, t = _make("expansion", "0", "Demo Store Charlie (new location)", "D. Example", 90, 8, 10)
    _finish(c, t, _d(20), "lease")
    storage.save_case(c)
    _make("renewal", "103", "Demo Store Delta", "E. Example", 5, 2, 1)
    _make("transfer", "104", "Demo Store Echo", "F. Example", 25, 11, 16, None, "G. Sample")
    print(f"Added {len(storage.list_cases())} demo cases.")


if __name__ == "__main__":
    main()
