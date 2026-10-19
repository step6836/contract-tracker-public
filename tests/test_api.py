"""Smoke tests: run with  python -m pytest tests -q"""
import os
import tempfile
from datetime import date, timedelta

os.environ["TRACKER_DATA_DIR"] = tempfile.mkdtemp()

import pytest
from fastapi.testclient import TestClient

from backend import auth, main


@pytest.fixture(scope="module")
def client():
    auth.create_user("step", "Step", "correct-horse-1")
    auth.create_user("teammate", "Teammate", "battery-staple-2")
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "step", "password": "nope"}).status_code == 401
    assert c.get("/api/cases").status_code == 401
    assert c.post("/api/login", json={"username": "step", "password": "correct-horse-1"}).status_code == 200
    return c


def d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


RENEWAL_DATES = {"fa_expiration": d(-150), "election_due": d(-60)}


def mk(client, type_, **kw):
    body = {"type": type_, "store_number": "12", "store_name": "Test Store", "start_date": d(20),
            **({"dates": RENEWAL_DATES} if type_ == "renewal" else {}), **kw}
    r = client.post("/api/cases", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def mk_status(client, type_, **kw):
    body = {"type": type_, "store_number": "12", "store_name": "Test Store", "start_date": d(20), **kw}
    return client.post("/api/cases", json=body).status_code


def finish(client, cid, step, on):
    """Complete a step: checklist steps finish when their last box is checked, others via Mark complete."""
    if not step["checklist"]:
        return client.post(f"/api/cases/{cid}/steps/{step['id']}/complete", json={"completed_on": on})
    for ci in step["checklist"]:
        r = client.post(f"/api/cases/{cid}/steps/{step['id']}/check", json={"key": ci["key"], "checked": True, "checked_on": on})
        assert r.status_code == 200, r.text
    return r


def test_renewal_order_and_checklist(client):
    c = mk(client, "renewal")
    assert c["id"] == "R-12-1"
    assert c["steps"][0]["status"] == "done"          # start step done on the start date
    assert next(s for s in c["steps"] if s["id"] == "mgmt_confirmation")["hidden"]   # only after a hold
    # out-of-order completion is rejected
    r = client.post(f"/api/cases/{c['id']}/steps/fai/complete", json={})
    assert r.status_code == 400 and "in order" in r.json()["detail"]
    # date before previous step is rejected
    r = client.post(f"/api/cases/{c['id']}/steps/election_form_received/complete", json={"completed_on": d(30)})
    assert r.status_code == 400
    # future date rejected
    r = client.post(f"/api/cases/{c['id']}/steps/election_form_received/complete",
                    json={"completed_on": (date.today() + timedelta(days=2)).isoformat()})
    assert r.status_code == 400
    r = client.post(f"/api/cases/{c['id']}/steps/election_form_received/complete", json={"completed_on": d(15)})
    assert r.status_code == 200
    v = r.json()
    assert v["current"]["id"] == "contracts_eval" and v["current"]["days_in_step"] == 15
    # the evaluation: boxes go in order, each on a date, and the last one completes the step
    ev = f"/api/cases/{c['id']}/steps/contracts_eval"
    chk = lambda key, on=None, checked=True: client.post(f"{ev}/check", json={"key": key, "checked": checked, "checked_on": on})
    assert chk("proceed_confirmed", d(5)).status_code == 400          # Legal first
    assert chk("sent_to_legal", d(16)).status_code == 400                # before the previous step
    assert chk("bogus").status_code == 400
    assert client.post(f"/api/cases/{c['id']}/steps/fai/check", json={"key": "sent_to_legal", "checked": True}).status_code == 400
    v = chk("sent_to_legal", d(10)).json()
    assert v["current"]["id"] == "contracts_eval" and v["current"]["checklist"][0]["date"] == d(10)
    listed = next(x for x in client.get("/api/cases").json() if x["id"] == c["id"])
    assert listed["current"]["status_note"] == "Waiting on confirmation to proceed"
    r = client.post(f"{ev}/complete", json={})
    assert r.status_code == 400 and "Confirmation to proceed" in r.json()["detail"]
    assert chk("proceed_confirmed", d(12)).status_code == 400         # before it was sent to Legal
    v = chk("proceed_confirmed", d(4)).json()
    ev_step = next(s for s in v["steps"] if s["id"] == "contracts_eval")
    assert ev_step["status"] == "done" and ev_step["date"] == d(4)
    assert [ci["date"] for ci in ev_step["checklist"]] == [d(10), d(4)]
    assert v["current"]["id"] == "fdd_nda" and v["current"]["days_in_step"] == 4
    side = v["side_track"]
    assert side["started"] and side["current"]["id"] == "ops_acct_inspection" and side["current"]["days_in_step"] == 4
    # undo reopens the evaluation waiting on confirmation, keeping the Legal date
    v = client.post(f"/api/cases/{c['id']}/undo", json={}).json()
    assert v["current"]["id"] == "contracts_eval"
    assert [ci["date"] for ci in v["current"]["checklist"]] == [d(10), None]
    assert chk("sent_to_legal", checked=False).status_code == 200


def inspect(client, cid, on):
    """Request both renewal inspections and mark them satisfactory, all on `on`."""
    for key in ("acct_requested", "acct_satisfactory", "ops_requested", "ops_satisfactory"):
        r = client.post(f"/api/cases/{cid}/steps/ops_acct_inspection/check",
                        json={"key": key, "checked": True, "checked_on": on, "track": "side"})
        assert r.status_code == 200, r.text
    return r


def test_inspection_runs_alongside_fdd(client):
    c = mk(client, "renewal", store_number="41")
    cid = c["id"]
    assert [s["id"] for s in c["steps"]][3:5] == ["mgmt_confirmation", "fdd_nda"]
    assert [s["id"] for s in c["side_track"]["steps"]] == ["ops_acct_inspection"]
    ins = f"/api/cases/{cid}/steps/ops_acct_inspection/check"
    chk = lambda key, on=None, checked=True: client.post(ins, json={"key": key, "checked": checked, "checked_on": on, "track": "side"})
    # the inspection waits for the Contracts Team Evaluation
    assert not c["side_track"]["started"] and c["side_track"]["current"] is None
    r = chk("acct_requested", d(18))
    assert r.status_code == 400 and "Contracts Team Evaluation" in r.json()["detail"]
    finish(client, cid, next(s for s in c["steps"] if s["id"] == "election_form_received"), d(18))
    v = finish(client, cid, next(s for s in c["steps"] if s["id"] == "contracts_eval"), d(15)).json()
    assert v["current"]["id"] == "fdd_nda" and v["side_track"]["current"]["id"] == "ops_acct_inspection"
    assert chk("ops_requested", d(16)).status_code == 400               # before the evaluation was done
    # each group has its own order, but the groups don't wait on each other
    assert chk("acct_satisfactory", d(10)).status_code == 400
    assert chk("ops_requested", d(14)).status_code == 200
    assert chk("acct_requested", d(13)).status_code == 200
    listed = next(x for x in client.get("/api/cases").json() if x["id"] == cid)
    assert listed["current"]["id"] == "fdd_nda"
    assert listed["side"]["alongside"] and listed["side"]["current"]["status_note"] == "Accounting: requested · Operations: requested"
    # the FDD & NDA go ahead at the same time
    fdd = lambda key, on: client.post(f"/api/cases/{cid}/steps/fdd_nda/check", json={"key": key, "checked": True, "checked_on": on})
    assert fdd("fdd_sent", d(14)).status_code == 200
    v = chk("ops_satisfactory", d(6)).json()
    assert v["side_track"]["current"]["status_note"] == "Accounting: requested · Operations: satisfactory"
    # the last box completes the step, dated on the latest box (not necessarily the last one clicked)
    v = chk("acct_satisfactory", d(8)).json()
    step = v["side_track"]["steps"][0]
    assert step["status"] == "done" and step["date"] == d(6) and v["side_track"]["done"]
    assert v["current"]["id"] == "fdd_nda"
    assert not any(s["label"] == "VP Approval" for s in v["steps"])   # renewals have no VP approval
    # the evaluation can't be undone while the inspection it started has progress
    v = client.post(f"/api/cases/{cid}/steps/fdd_nda/check", json={"key": "fdd_sent", "checked": False}).json()
    r = client.post(f"/api/cases/{cid}/undo", json={})
    assert r.status_code == 400 and "Undo the Operations & Accounting Team Inspection first" in r.json()["detail"]
    v = client.post(f"/api/cases/{cid}/undo", json={"track": "side"}).json()
    assert {ci["key"]: ci["checked"] for ci in v["side_track"]["current"]["checklist"]} == {
        "acct_requested": True, "acct_satisfactory": True, "ops_requested": True, "ops_satisfactory": False}
    chk("ops_satisfactory", d(6))
    # FDD & NDA: the 14-day review starts once they're signed and returned; receiving them moves the case on
    assert "review_ends" not in v["current"]
    fdd = lambda key, on: client.post(f"/api/cases/{cid}/steps/fdd_nda/check", json={"key": key, "checked": True, "checked_on": on})
    v = fdd("fdd_sent", d(5)).json()
    assert "review_ends" not in v["current"] and v["current"]["status_note"] == "Waiting on signed FDD & NDA"
    v = fdd("fdd_received", d(2)).json()
    assert v["current"]["id"] == "fai"
    assert next(s for s in v["steps"] if s["id"] == "fdd_nda")["review_ends"] == d(-12)   # still running
    v = client.post(f"/api/cases/{cid}/undo", json={}).json()
    assert v["current"]["id"] == "fdd_nda" and v["current"]["checklist"][0]["checked"]


def test_renewal_fai_sections(client):
    c = mk(client, "renewal", store_number="63")
    cid = c["id"]
    assert not any(s["id"].startswith("lease_") for s in c["steps"])     # lease steps folded into FAI
    for s in c["steps"]:
        if s["id"] == "fai":
            break
        if s["status"] == "pending":
            finish(client, cid, s, d(15))
    url = f"/api/cases/{cid}/steps/fai"
    sec = lambda section, node, on=None, **kw: client.post(f"{url}/section", json={"section": section, "node": node, "done_on": on, **kw})
    # nothing opens until the FAI is sent
    r = sec("personal", "docs", item="ids")
    assert r.status_code == 400 and "FAI Sent" in r.json()["detail"]
    client.post(f"{url}/check", json={"key": "fai_sent", "checked": True, "checked_on": d(12)})
    # personal: four boxes, one completion date
    for item in ("ids", "residence", "bank"):
        v = sec("personal", "docs", item=item).json()
    assert v["current"]["status_note"] == "Waiting on personal documents, entity documents, lease"
    assert sec("personal", "docs", d(13), item="tax_return").status_code == 400   # before FAI was sent
    v = sec("personal", "docs", d(9), item="tax_return").json()
    personal = next(x for x in v["current"]["sections"] if x["key"] == "personal")
    assert personal["done"] and personal["done_on"] == d(9)
    assert client.post(f"{url}/check", json={"key": "fai_sent", "checked": False}).status_code == 400
    # entity: pick LLC, which shows the LLC list; switching to INC swaps the list and clears the ticks
    assert sec("entity", "llc_docs", item="llc_articles").status_code == 400      # choose a type first
    sec("entity", "type", value="llc")
    sec("entity", "llc_docs", item="llc_articles")
    v = sec("entity", "type", value="inc").json()
    entity = next(x for x in v["current"]["sections"] if x["key"] == "entity")
    assert [n["key"] for n in entity["nodes"]] == ["type", "inc_docs"]
    assert not any(i["checked"] for i in entity["nodes"][1]["items"])
    sec("entity", "type", value="llc")
    # the lease doesn't hold FAI up: once the personal and entity documents are in, the FA draft is next
    v = sec("lease", "on_file", value="no").json()
    lease = next(x for x in v["current"]["sections"] if x["key"] == "lease")
    assert lease["nodes"][1]["instruction"] == "Request lease"
    assert [i["label"] for i in lease["nodes"][1]["items"]] == ["Lease received", "Lease satisfactory"]
    for item in ("llc_articles", "llc_operating", "llc_ownership", "llc_irs", "llc_tax"):
        v = sec("entity", "llc_docs", d(7), item=item).json()
    fai = next(s for s in v["steps"] if s["id"] == "fai")
    assert fai["status"] == "done" and fai["date"] == d(7) and v["current"]["id"] == "fa_draft"
    assert fai["carry_on_open"]
    # ...and keeps going after FAI is done: on file but doesn't cover the term, so request the renewal draft
    sec("lease", "on_file", value="yes")
    v = sec("lease", "covers_term", value="no").json()
    assert v["current"]["id"] == "fa_draft"
    assert sec("lease", "request_draft", item="draft_satisfactory").status_code == 400   # receive it first
    sec("lease", "request_draft", item="draft_received")
    v = sec("lease", "request_draft", d(5), item="draft_satisfactory").json()
    fai = next(s for s in v["steps"] if s["id"] == "fai")
    lease = next(x for x in fai["sections"] if x["key"] == "lease")
    assert lease["done_text"] == "Lease received & approved" and lease["done_on"] == d(5)
    assert fai["date"] == d(7) and v["current"]["id"] == "fa_draft"                      # FAI's date is unchanged
    # undo reopens the entity documents (what finished FAI), not the lease
    v = client.post(f"/api/cases/{cid}/undo", json={}).json()
    done = {x["key"]: x["done"] for x in v["current"]["sections"]}
    assert v["current"]["id"] == "fai" and done == {"personal": True, "entity": False, "lease": True}


def fai_done_case(client, store):
    """A renewal walked through FAI (LLC, lease on file covering the term), sitting at the FA draft."""
    c = mk(client, "renewal", store_number=store, start_date=d(30))
    cid = c["id"]
    for s in c["steps"]:
        if s["id"] == "fai":
            break
        if s["status"] == "pending":
            finish(client, cid, s, d(25))
    inspect(client, cid, d(24))
    url = f"/api/cases/{cid}/steps/fai"
    client.post(f"{url}/check", json={"key": "fai_sent", "checked": True, "checked_on": d(24)})
    sec = lambda section, node, **kw: client.post(f"{url}/section", json={"section": section, "node": node, "done_on": d(20), **kw})
    for item in ("ids", "residence", "bank", "tax_return"):
        sec("personal", "docs", item=item)
    sec("entity", "type", value="llc")
    for item in ("llc_articles", "llc_operating", "llc_ownership", "llc_irs", "llc_tax"):
        sec("entity", "llc_docs", item=item)
    sec("lease", "on_file", value="yes")
    v = sec("lease", "covers_term", value="yes").json()
    assert v["current"]["id"] == "fa_draft"
    return cid


def test_renewal_fa_draft_and_review(client):
    cid = fai_done_case(client, "71")
    fa = f"/api/cases/{cid}/steps/fa_draft"
    chk = lambda key, on: client.post(f"{fa}/check", json={"key": key, "checked": True, "checked_on": on})
    v = client.get(f"/api/cases/{cid}").json()
    assert "can_correct" in v["current"] and not v["current"]["can_correct"]   # request the draft first
    v = chk("draft_requested", d(18)).json()
    assert v["current"]["status_note"] == "Legal drafting Franchise Agreement" and v["current"]["can_correct"]
    chk("draft_received", d(12))
    assert client.post(f"{fa}/correction", json={}).status_code == 400          # too late once received
    v = chk("draft_sent", d(10)).json()
    assert v["current"]["id"] == "fa_negotiation"
    assert v["current"]["review_ends"] == d(3)                                   # 7 days from sending
    assert v["current"]["started_by"] == {"label": "Draft sent to franchisee", "date": d(10)}
    fa_step = next(s for s in v["steps"] if s["id"] == "fa_draft")
    assert next(c for c in fa_step["checklist"] if c["key"] == "draft_sent")["hide_when_done"]


def draft_sent_case(client, store, sent_on):
    cid = fai_done_case(client, store)
    for key in ("draft_requested", "draft_received", "draft_sent"):
        r = client.post(f"/api/cases/{cid}/steps/fa_draft/check", json={"key": key, "checked": True, "checked_on": sent_on})
    assert r.json()["current"]["id"] == "fa_negotiation"
    return cid


def test_renewal_signing_scheduled_after_review(client):
    # Review still running (draft sent today): signing can be booked, but only once the 7 days are up.
    cid = draft_sent_case(client, "72", d(0))
    url = f"/api/cases/{cid}/steps/fa_negotiation/schedule"
    r = client.post(url, json={"on": d(-3), "at": "14:00"})
    assert r.status_code == 400 and "review period isn't over" in r.json()["detail"]
    v = client.post(url, json={"on": d(-8), "at": "14:00"}).json()
    assert v["current"]["id"] == "fa_negotiation"                               # timer keeps going
    assert v["current"]["schedule"]["at"] == f"{d(-8)}T14:00"
    assert v["current"]["status_note"].startswith("Signing scheduled for")

    # Review over (sent 10 days ago, ended 3 days ago): booking moves the case on to the signing.
    cid = draft_sent_case(client, "73", d(10))
    v = client.get(f"/api/cases/{cid}").json()
    assert v["current"]["review_over"] and "Schedule the signing" in v["attention"][0]["text"]
    v = client.post(f"/api/cases/{cid}/steps/fa_negotiation/schedule", json={"on": d(2), "at": "14:00"}).json()
    assert next(s for s in v["steps"] if s["id"] == "fa_negotiation")["date"] == d(3)
    sc = v["current"]["schedule"]
    assert v["current"]["id"] == "fa_signing" and sc["passed"] and sc["question"] == "Did signing happen?"
    sign = f"/api/cases/{cid}/steps/fa_signing"
    r = client.post(f"{sign}/happened", json={"happened": True})
    assert r.status_code == 400 and "Calendar invitation sent" in r.json()["detail"]
    v = client.post(f"{sign}/check", json={"key": "invite_sent", "checked": True, "checked_on": d(3)}).json()
    assert v["current"]["id"] == "fa_signing"                                     # the box alone doesn't sign it
    v = client.post(f"{sign}/happened", json={"happened": False}).json()          # didn't happen: rebook
    assert v["current"]["id"] == "fa_signing" and v["current"]["schedule"]["at"] is None
    assert client.post(f"{sign}/schedule", json={"on": d(-5), "at": "10:00"}).status_code == 200
    assert client.post(f"{sign}/happened", json={"happened": True}).status_code == 400   # not yet
    client.post(f"{sign}/schedule", json={"on": d(1), "at": "09:30"})
    v = client.post(f"{sign}/happened", json={"happened": True}).json()
    assert v["signing_date"] == d(1) and v["current"]["id"] == "after_signing"
    after = f"/api/cases/{cid}/steps/after_signing/check"
    assert client.post(after, json={"key": "contact_master", "checked": True}).status_code == 200   # any order
    v = client.post(after, json={"key": "supplier_reminder", "checked": True}).json()
    assert v["status"] == "completed"


def test_renewal_fai_correction_loop(client):
    cid = fai_done_case(client, "72")
    fa, fai = f"/api/cases/{cid}/steps/fa_draft", f"/api/cases/{cid}/steps/fai"
    client.post(f"{fa}/check", json={"key": "draft_requested", "checked": True, "checked_on": d(18)})
    assert client.post(f"{fa}/correction", json={"on": d(19)}).status_code == 400   # before the request
    v = client.post(f"{fa}/correction", json={"on": d(15)}).json()
    # back at FAI in correction mode, no issues marked yet, FA draft wiped
    assert v["current"]["id"] == "fai" and v["current"]["correction"]["open_issues"] == 0
    assert v["current"]["days_in_step"] == 15
    assert v["current"]["status_note"] == "FAI & Lease Confirmation correction: mark the documents with issues"
    fa_step = next(s for s in v["steps"] if s["id"] == "fa_draft")
    assert not any(ci["checked"] for ci in fa_step["checklist"]) and fa_step["corrections"][0]["resolved"] is None
    # normal FAI editing is locked while correcting
    assert client.post(f"{fai}/section", json={"section": "personal", "node": "docs", "item": "ids", "checked": False}).status_code == 400
    iss = lambda section, item, checked, on=None: client.post(f"{fai}/issue", json={"section": section, "item": item, "checked": checked, "on": on})
    assert iss("lease", "on_file", True).status_code == 400                       # the lease has no issue boxes
    iss("personal", "bank", True)
    v = iss("entity", "llc_irs", True).json()
    assert v["current"]["status_note"] == "FAI & Lease Confirmation correction: 2 open issues"
    v = iss("personal", "bank", False).json()
    assert v["current"]["id"] == "fai"
    # clearing the last issue finishes FAI again and returns to a fresh FA draft
    v = iss("entity", "llc_irs", False, d(11)).json()
    assert v["current"]["id"] == "fa_draft" and v["current"]["days_in_step"] == 11
    assert next(s for s in v["steps"] if s["id"] == "fai")["date"] == d(11)
    assert v["current"]["corrections"] == [{"started": d(15), "resolved": d(11)}]
    # undo reopens the correction with that last issue flagged again; undo again cancels it entirely
    v = client.post(f"/api/cases/{cid}/undo", json={}).json()
    assert v["current"]["id"] == "fai" and v["current"]["correction"]["open_issues"] == 1
    v = client.post(f"/api/cases/{cid}/undo", json={}).json()
    assert v["current"]["id"] == "fa_draft" and v["current"]["corrections"] == []
    assert v["current"]["checklist"][0]["date"] == d(18)                           # the original request is back
    assert next(s for s in v["steps"] if s["id"] == "fai")["date"] == d(20)


def test_transfer_existing_franchisee_waivers_and_effective_date(client):
    c = mk(client, "transfer", store_number="97", transferee="Someone", flags={"transferee_is_existing": True})
    skipped = {s["id"] for s in c["steps"] if s["status"] == "skipped"}
    assert skipped == {"interview"}
    app = lambda case: next(s for s in case["steps"] if s["id"] == "app_request")
    assert [g["key"] for g in app(c)["groups"]] == ["app"]                  # existing: just the application
    c2 = mk(client, "transfer", store_number="98")
    assert not [s for s in c2["steps"] if s["status"] == "skipped"]
    assert [g["key"] for g in app(c2)["groups"]] == ["app", "bg", "quest"]   # new buyer: all three come back
    assert c2["steps"][1]["label"] == "Contracts Evaluation (Transferor)"
    assert c2["steps"][0]["status"] == "pending"                              # waits for VP notified
    labels = [s["label"] for s in c["steps"]]
    assert labels[labels.index("Operations & Accounting Team Inspection") + 1] == "VP Approval"
    assert [s["id"] for s in c["steps"]][-5:] == ["fai", "fa_draft", "fa_negotiation", "fa_signing", "after_signing"]

    # existing franchisee: straight from FDD & NDA to FAI
    v = walk_transfer_to(client, c["id"], "fdd_nda")
    v = finish(client, c["id"], v["current"], d(14)).json()
    assert v["current"]["id"] == "fai"

    # new buyer: the interview is booked once the FDD & NDA are back (no waiting for the review period),
    # "Yes, it happened" ticks Interview held, and VP confirmation finishes it
    cid = c2["id"]
    v = walk_transfer_to(client, cid, "fdd_nda")
    v = finish(client, cid, v["current"], d(14)).json()
    assert v["current"]["id"] == "interview" and v["current"]["status_note"] == "Interview not scheduled yet"
    iv = f"/api/cases/{cid}/steps/interview"
    assert client.post(f"{iv}/check", json={"key": "interview_held", "checked": True, "checked_on": d(13)}).status_code == 400
    assert client.post(f"{iv}/schedule", json={"on": d(13), "at": "10:00"}).status_code == 200
    v = client.post(f"{iv}/happened", json={"happened": True}).json()
    assert v["current"]["id"] == "interview" and v["current"]["schedule"]["question"] is None
    assert v["current"]["status_note"] == "Waiting on interview summary & evaluation"
    for key in ("summary_eval", "eval_sent_vp", "vp_confirmed"):
        v = client.post(f"{iv}/check", json={"key": key, "checked": True, "checked_on": d(12)}).json()
    assert v["current"]["id"] == "fai"

    # FAI (lease: negotiation, draft, approval), FA draft, review, signing, after signing
    fai = f"/api/cases/{cid}/steps/fai"
    sec = lambda section, node, **kw: client.post(f"{fai}/section", json={"section": section, "node": node, "done_on": d(11), **kw})
    client.post(f"{fai}/check", json={"key": "fai_sent", "checked": True, "checked_on": d(12)})
    for item in ("ids", "residence", "bank", "tax_return"):
        sec("personal", "docs", item=item)
    sec("entity", "type", value="inc")
    for item in ("inc_articles", "inc_bylaws", "inc_ownership", "inc_irs", "inc_tax"):
        sec("entity", "inc_docs", item=item)
    assert sec("lease", "lease_steps", item="lease_approved", checked=True).status_code == 400   # in order
    for item in ("lease_negotiation", "lease_draft", "lease_approved"):
        v = sec("lease", "lease_steps", item=item, checked=True).json()
    assert v["current"]["id"] == "fa_draft"
    fa = lambda key, on: client.post(f"/api/cases/{cid}/steps/fa_draft/check", json={"key": key, "checked": True, "checked_on": on})
    fa("draft_requested", d(11))
    assert fa("sent_transferee", d(11)).status_code == 400                     # not before the draft is received
    fa("draft_received", d(11))
    v = fa("sent_transferee", d(11)).json()                                     # either side first
    assert v["current"]["id"] == "fa_draft" and v["current"]["status_note"] == "Waiting on draft to be sent to the transferor"
    assert client.post(f"/api/cases/{cid}/steps/fa_draft/check", json={"key": "draft_received", "checked": False}).status_code == 400
    v = fa("sent_transferor", d(10)).json()
    assert v["current"]["id"] == "fa_negotiation"
    assert v["current"]["started_by"] == {"label": "Draft sent to transferor & transferee", "date": d(10)}
    assert v["current"]["review_ends"] == d(3)                                  # 7 days from the later send
    v = client.post(f"/api/cases/{cid}/steps/fa_negotiation/schedule", json={"on": d(2), "at": "14:00"}).json()
    v = client.post(f"/api/cases/{cid}/steps/fa_signing/happened", json={"happened": True}).json()
    assert v["signing_date"] == d(2) and v["effective_date"] is not None and v["current"]["id"] == "after_signing"
    for key in ("contact_master", "opening_date", "supplier_contacted"):                               # any order
        v = client.post(f"/api/cases/{cid}/steps/after_signing/check", json={"key": key, "checked": True}).json()
    assert v["status"] == "completed"


def walk_transfer_to(client, cid, stop):
    """Finish a transfer's steps (ticking every box) until the step `stop` is current."""
    v = client.get(f"/api/cases/{cid}").json()
    while v["current"]["id"] != stop:
        r = finish(client, cid, v["current"], d(15))
        assert r.status_code == 200, r.text
        v = r.json()
    return v


def test_expansion_steps_units_and_lease_side_track(client):
    c = mk(client, "expansion", store_number="0", units=[
        {"store_number": "41", "store_name": "Unit A"}, {"store_number": "", "store_name": ""},
        {"store_number": "42", "store_name": "Unit B"}])
    cid = c["id"]
    assert c["units"] == [{"store_number": "41", "store_name": "Unit A"}, {"store_number": "42", "store_name": "Unit B"}]
    assert mk_status(client, "expansion", units=[{"store_number": "4x", "store_name": "Bad"}]) == 400
    assert [s["id"] for s in c["steps"]] == [
        "site_approval_received", "contracts_eval", "ops_acct_inspection", "mgmt_approval", "app_request",
        "fdd_nda", "ops_interview", "fai", "fa_draft", "fa_negotiation", "fa_signing", "after_signing"]
    assert c["steps"][0]["status"] == "pending"                                # waits for Management notified
    v = client.patch(f"/api/cases/{cid}", json={"units": [{"store_number": "43", "store_name": "Unit C"}]}).json()
    assert v["units"] == [{"store_number": "43", "store_name": "Unit C"}]

    # lease can move before the main flow does
    assert c["side_track"]["current"]["id"] == "lease_negotiation"
    r = client.post(f"/api/cases/{cid}/steps/lease_negotiation/complete", json={"track": "lease", "completed_on": d(3)})
    assert r.status_code == 200
    for step in ("lease_draft", "lease_approval"):
        r = client.post(f"/api/cases/{cid}/steps/{step}/complete", json={"track": "lease"})
        assert r.status_code == 200
    assert r.json()["side_track"]["done"] and r.json()["status"] == "active"

    v = client.get(f"/api/cases/{cid}").json()
    while v["current"]["id"] != "ops_interview":
        v = finish(client, cid, v["current"], d(15)).json()
    iv = f"/api/cases/{cid}/steps/ops_interview"
    client.post(f"{iv}/schedule", json={"on": d(14), "at": "10:00"})
    v = client.post(f"{iv}/happened", json={"happened": True}).json()
    assert v["current"]["id"] == "fai"
    assert [x["key"] for x in v["current"]["sections"]] == ["personal", "entity"]  # the lease has its own track
    fai = f"/api/cases/{cid}/steps/fai"
    sec = lambda section, node, **kw: client.post(f"{fai}/section", json={"section": section, "node": node, "done_on": d(12), **kw})
    client.post(f"{fai}/check", json={"key": "fai_sent", "checked": True, "checked_on": d(13)})
    for item in ("ids", "residence", "bank", "tax_return"):
        sec("personal", "docs", item=item)
    sec("entity", "type", value="llc")
    for item in ("llc_articles", "llc_operating", "llc_ownership", "llc_irs", "llc_tax"):
        v = sec("entity", "llc_docs", item=item).json()
    assert v["current"]["id"] == "fa_draft"
    for key in ("draft_requested", "draft_received", "draft_sent"):
        v = client.post(f"/api/cases/{cid}/steps/fa_draft/check", json={"key": key, "checked": True, "checked_on": d(11)}).json()
    assert v["current"]["id"] == "fa_negotiation" and v["current"]["review_ends"] == d(4)
    client.post(f"/api/cases/{cid}/steps/fa_negotiation/schedule", json={"on": d(2), "at": "14:00"})
    v = client.post(f"/api/cases/{cid}/steps/fa_signing/happened", json={"happened": True}).json()
    assert v["signing_date"] == d(2) and v["current"]["id"] == "after_signing"
    for key in ("contact_master", "opening_date", "staff_reminder", "supplier_contacted"):          # any order
        v = client.post(f"/api/cases/{cid}/steps/after_signing/check", json={"key": key, "checked": True}).json()
    assert v["status"] == "completed"
    # undo brings it back to active
    assert client.post(f"/api/cases/{cid}/undo", json={}).json()["status"] == "active"


def test_stale_followups_and_lease_deadline_show_in_attention(client):
    c = mk(client, "transfer", store_number="55", start_date=d(60))
    cases = {x["id"]: x for x in client.get("/api/cases").json()}
    kinds = {a["kind"] for a in cases[c["id"]]["attention"]}
    assert "stale" in kinds
    v = client.post(f"/api/cases/{c['id']}/notes",
                    json={"text": "Call franchisee back", "follow_up_date": d(2)}).json()
    assert any(a["kind"] == "follow_up" and a["level"] == "alert" for a in v["attention"])
    nid = v["notes"][0]["id"]
    v = client.post(f"/api/cases/{c['id']}/notes/{nid}/resolve").json()
    assert not any(a["kind"] == "follow_up" for a in v["attention"])


def test_close_reopen_and_event_log(client):
    c = mk(client, "transfer", store_number="77")
    assert client.post(f"/api/cases/{c['id']}/close", json={"outcome": "withdrawn", "reason": ""}).status_code == 400
    v = client.post(f"/api/cases/{c['id']}/close", json={"outcome": "not_approved", "reason": "Ops denied"}).json()
    assert v["status"] == "not_approved" and v["attention"] == []
    assert client.post(f"/api/cases/{c['id']}/steps/contracts_eval/complete", json={}).status_code == 400
    assert client.post(f"/api/cases/{c['id']}/reopen").json()["status"] == "active"
    ev = client.get("/api/events", params={"case_id": c["id"]}).json()
    assert [e["action"] for e in ev][:2] == ["case_reopened", "case_closed"]
    assert ev[0]["display_name"] == "Step"


def test_edit_step_date_and_ids_increment(client):
    a = mk(client, "renewal", store_number="31")
    b = mk(client, "renewal", store_number="31")
    assert (a["id"], b["id"]) == ("R-31-1", "R-31-2")
    client.post(f"/api/cases/{a['id']}/steps/election_form_received/complete", json={"completed_on": d(15)})
    assert client.post(f"/api/cases/{a['id']}/steps/election_form_received/date",
                       json={"new_date": d(19)}).status_code == 200
    assert client.post(f"/api/cases/{a['id']}/steps/election_form_received/date",
                       json={"new_date": d(40)}).status_code == 400


def test_hold_pauses_the_clock_and_brings_in_management_confirmation(client):
    c = mk(client, "renewal", store_number="81", start_date=d(30))
    cid = c["id"]
    assert c["target_signing_date"] == RENEWAL_DATES["fa_expiration"]          # defaults to the FA expiration
    v = finish(client, cid, c["current"], d(20)).json()                         # Election Form received
    assert v["current"]["id"] == "contracts_eval" and v["current"]["owner"] == "Contracts"
    hold = lambda **kw: client.post(f"/api/cases/{cid}/hold", json=kw)
    assert hold(reason="  ").status_code == 400                                 # a reason is required
    assert hold(reason="x", on=d(25)).status_code == 400                        # before the step started
    v = hold(reason="Major defaults at unit 12", on=d(15)).json()
    assert v["hold"]["step_label"] == "Contracts Team Evaluation" and v["hold"]["days"] == 15
    assert hold(reason="again").status_code == 400
    assert v["current"]["days_in_step"] == 5 and v["current"]["days_on_hold"] == 15   # 20 days in, 15 on hold
    assert not v["current"]["is_stale"]
    mc = next(s for s in v["steps"] if s["id"] == "mgmt_confirmation")
    assert not mc["hidden"] and mc["status"] == "pending"
    listed = next(x for x in client.get("/api/cases").json() if x["id"] == cid)
    assert listed["hold"]["reason"] == "Major defaults at unit 12"
    # boxes can still be ticked while on hold; the team moves to Legal once it's sent
    v = client.post(f"/api/cases/{cid}/steps/contracts_eval/check", json={"key": "sent_to_legal", "checked": True, "checked_on": d(14)}).json()
    assert v["current"]["owner"] == "Legal"
    assert client.post(f"/api/cases/{cid}/resume", json={"on": d(16)}).status_code == 400
    v = client.post(f"/api/cases/{cid}/resume", json={"on": d(5)}).json()
    assert v["hold"] is None and v["holds"][0]["days"] == 10 and v["holds"][0]["end"] == d(5)
    assert v["current"]["days_in_step"] == 10                                   # 20 days in, 10 on hold
    v = client.post(f"/api/cases/{cid}/steps/contracts_eval/check", json={"key": "proceed_confirmed", "checked": True, "checked_on": d(4)}).json()
    assert v["current"]["id"] == "mgmt_confirmation" and v["current"]["owner"] == "Management"
    assert next(s for s in v["steps"] if s["id"] == "contracts_eval")["holds"][0]["reason"] == "Major defaults at unit 12"
    ev = client.get("/api/events", params={"case_id": cid}).json()
    assert {"case_on_hold", "case_resumed"} <= {e["action"] for e in ev}
    # a case that was never held skips Management Confirmation
    c2 = mk(client, "renewal", store_number="82")
    v = finish(client, c2["id"], c2["current"], d(15)).json()
    v = finish(client, c2["id"], v["current"], d(14)).json()
    assert v["current"]["id"] == "fdd_nda" and v["side_track"]["current"]["owner"] == "Contracts"
    ins = f"/api/cases/{c2['id']}/steps/ops_acct_inspection/check"
    for key in ("acct_requested", "acct_satisfactory", "ops_requested"):
        v = client.post(ins, json={"key": key, "checked": True, "checked_on": d(10), "track": "side"}).json()
    assert v["side_track"]["current"]["owner"] == "Operations"                  # Accounting is satisfied


def test_election_form_reminders_and_fa_expiration(client):
    c = mk(client, "renewal", store_number="83", start_date=d(100),
           dates={"fa_expiration": d(-80), "election_due": d(-20)})
    cid = c["id"]
    assert mk_status(client, "renewal", dates={"fa_expiration": d(-80)}) == 400          # due date required
    rem = c["current"]["reminders"]
    assert rem["due"] == d(-20) and [r["from"] for r in rem["items"]] == [engine_months_before(d(-20)), d(-13)]
    texts = [a["text"] for a in c["attention"]]
    assert any(t.startswith("1st reminder: Contracts Team follow-up") for t in texts)  # inside the last month
    assert not any(t.startswith("2nd reminder") for t in texts)                         # not inside the last week
    v = client.post(f"/api/cases/{cid}/steps/election_form_received/reminder", json={"key": "reminder_1", "done": True}).json()
    assert not any(a["kind"] == "reminder" for a in v["attention"])
    assert not v["current"]["is_stale"]                                                  # no clock on this step
    # past the due date it's overdue
    v = client.patch(f"/api/cases/{cid}", json={"dates": {"election_due": d(3)}}).json()
    assert any(a["kind"] == "due_date" and a["level"] == "alert" for a in v["attention"])
    assert client.patch(f"/api/cases/{cid}", json={"dates": {"election_due": None}}).status_code == 400
    # the FA term passing before signing is flagged
    v = client.patch(f"/api/cases/{cid}", json={"dates": {"fa_expiration": d(1)}}).json()
    assert any(a["kind"] == "expired" for a in v["attention"])


def engine_months_before(iso_day):
    from backend import engine
    return engine.iso(engine.add_months(date.fromisoformat(iso_day), -1))


def test_signing_waits_for_fdd_review_and_lease(client):
    c = mk(client, "renewal", store_number="84", start_date=d(40))
    cid = c["id"]
    v = c
    while v["current"]["id"] != "fdd_nda":
        v = finish(client, cid, v["current"], d(38)).json()
    fdd = f"/api/cases/{cid}/steps/fdd_nda/check"
    client.post(fdd, json={"key": "fdd_sent", "checked": True, "checked_on": d(12)})
    client.post(fdd, json={"key": "fdd_received", "checked": True, "checked_on": d(10)})   # review ends d(-4)
    fai = f"/api/cases/{cid}/steps/fai"
    sec = lambda section, node, **kw: client.post(f"{fai}/section", json={"section": section, "node": node, "done_on": d(9), **kw})
    client.post(f"{fai}/check", json={"key": "fai_sent", "checked": True, "checked_on": d(10)})
    for item in ("ids", "residence", "bank", "tax_return"):
        sec("personal", "docs", item=item)
    sec("entity", "type", value="llc")
    for item in ("llc_articles", "llc_operating", "llc_ownership", "llc_irs", "llc_tax"):
        v = sec("entity", "llc_docs", item=item).json()
    assert v["current"]["id"] == "fa_draft"                                         # lease still open
    assert v["current"]["owner"] == "Contracts"
    fa = lambda key: client.post(f"/api/cases/{cid}/steps/fa_draft/check", json={"key": key, "checked": True, "checked_on": d(8)}).json()
    assert fa("draft_requested")["current"]["owner"] == "Legal"
    fa("draft_received")
    v = fa("draft_sent")                                                              # 7-day review ends d(1)
    assert v["current"]["id"] == "fa_negotiation"
    sc = v["current"]["schedule"]
    assert "lease confirmation" in sc["blocked"] and sc["not_before"] == d(-4) and "FDD & NDA" in sc["not_before_why"]
    url = f"/api/cases/{cid}/steps/fa_negotiation/schedule"
    r = client.post(url, json={"on": d(-10), "at": "10:00"})
    assert r.status_code == 400 and "lease confirmation" in r.json()["detail"]
    sec("lease", "on_file", value="yes")
    v = sec("lease", "covers_term", value="yes").json()                              # lease done after FAI
    assert "Operations & Accounting Team Inspection" in v["current"]["schedule"]["blocked"]   # still out
    r = client.post(url, json={"on": d(-4), "at": "10:00"})
    assert r.status_code == 400 and "Inspection" in r.json()["detail"]
    v = inspect(client, cid, d(9)).json()
    assert v["current"]["schedule"]["blocked"] is None
    r = client.post(url, json={"on": d(-2), "at": "10:00"})                          # after the 7 days, inside the 14
    assert r.status_code == 400 and "14-day FDD & NDA review period" in r.json()["detail"]
    assert client.post(url, json={"on": d(-4), "at": "10:00"}).status_code == 200


def test_cases_from_before_the_renewal_dates_can_still_be_edited(client):
    c = mk(client, "renewal", store_number="85")
    from backend import storage
    with storage.LOCK:
        raw = storage.load_case(c["id"])
        raw.pop("dates")
        storage.save_case(raw)
    r = client.patch(f"/api/cases/{c['id']}", json={"box_url": "https://example.com", "dates": {"fa_expiration": None, "election_due": None}})
    assert r.status_code == 200, r.text
    v = client.patch(f"/api/cases/{c['id']}", json={"dates": {"election_due": d(-30)}}).json()
    assert v["dates"]["election_due"] == d(-30)


def test_notes_remember_the_step(client):
    c = mk(client, "renewal", store_number="86")
    v = finish(client, c["id"], c["current"], d(15)).json()
    v = client.post(f"/api/cases/{c['id']}/notes", json={"text": "Asked Legal for an update"}).json()
    assert v["notes"][-1]["step"] == "Contracts Team Evaluation"


def test_lease_case_follow_up_review_and_approval(client):
    r = client.post("/api/cases", json={"type": "lease", "store_number": "90", "store_name": "Lease Store", "start_date": d(10)})
    assert r.status_code == 400 and "Lease expires" in r.json()["detail"]            # the expiration is required
    c = mk(client, "lease", store_number="90", dates={"lease_expiration": d(-40)})
    cid = c["id"]
    assert cid == "L-90-1" and not c["has_signing"] and c["target_signing_date"] is None
    assert c["current"]["id"] == "draft_received" and c["current"]["owner"] == "Franchisee"
    assert any(a["kind"] == "expiring" for a in c["attention"])                       # inside 60 days
    sec = f"/api/cases/{cid}/steps/draft_received/section"
    v = client.post(sec, json={"section": "kind", "node": "kind", "value": "extension", "done_on": d(4)}).json()
    step = next(s for s in v["steps"] if s["id"] == "draft_received")
    assert step["status"] == "done" and step["date"] == d(4) and step["sections"][0]["done_text"] == "Lease extension"
    v = client.post(f"/api/cases/{cid}/steps/lease_review/check", json={"key": "lease_approved", "checked": True, "checked_on": d(1)}).json()
    assert v["status"] == "completed" and v["attention"] == []
    expired = mk(client, "lease", store_number="91", dates={"lease_expiration": d(3)})
    assert any(a["kind"] == "expired" and "isn't approved" in a["text"] for a in expired["attention"])


def test_lease_links_to_renewals_transfers_and_expansions(client):
    tr = mk(client, "transfer", store_number="95")
    lease = mk(client, "lease", store_number="95", dates={"lease_expiration": d(-150)})
    # same store, not linked yet: both sides are told
    assert [o["id"] for o in lease["related"]["suggested"]] == [tr["id"]]
    assert any(a["kind"] == "link" and tr["id"] in a["text"] for a in lease["attention"])
    listed = {x["id"]: x for x in client.get("/api/cases").json()}
    assert any(a["kind"] == "link" and lease["id"] in a["text"] for a in listed[tr["id"]]["attention"])
    # link from the transfer's side: it's kept on the lease and shows on both
    v = client.post(f"/api/cases/{tr['id']}/links", json={"case_id": lease["id"]}).json()
    assert v["id"] == tr["id"] and [o["id"] for o in v["related"]["linked"]] == [lease["id"]]
    assert v["related"]["linked"][0]["created_by_name"] == "Step" and not v["related"]["suggested"]
    assert not any(a["kind"] == "link" for a in v["attention"])
    lv = client.get(f"/api/cases/{lease['id']}").json()
    assert lv["links"] == [tr["id"]] and lv["related"]["linked"][0]["current"]["label"]
    assert client.post(f"/api/cases/{lease['id']}/links", json={"case_id": tr["id"]}).status_code == 400   # already
    # only leases link, and a lease doesn't link to a lease
    other = mk(client, "renewal", store_number="96")
    assert client.post(f"/api/cases/{tr['id']}/links", json={"case_id": other["id"]}).status_code == 400
    lease2 = mk(client, "lease", store_number="97", dates={"lease_expiration": d(-150)}, links=[other["id"]])
    assert [o["id"] for o in lease2["related"]["linked"]] == [other["id"]]
    assert client.post(f"/api/cases/{lease2['id']}/links", json={"case_id": lease["id"]}).status_code == 400
    assert mk_status(client, "lease", dates={"lease_expiration": d(-150)}, links=["R-0-404"]) == 404
    # unlink
    v = client.post(f"/api/cases/{lease['id']}/links", json={"case_id": tr["id"], "linked": False}).json()
    assert v["links"] == [] and [o["id"] for o in v["related"]["suggested"]] == [tr["id"]]
    ev = client.get("/api/events", params={"case_id": tr["id"]}).json()
    assert [e["action"] for e in ev][:2] == ["case_unlinked", "case_linked"]


def test_old_renewal_keeps_its_inspection_when_it_moves_to_its_own_track(client):
    c = mk(client, "renewal", store_number="98")
    from backend import storage
    with storage.LOCK:  # the way a renewal was saved before the inspection ran alongside
        raw = storage.load_case(c["id"])
        ins = {"id": "ops_acct_inspection", "label": "Operations & Accounting Team Inspection", "status": "done",
               "date": d(12), "by": "step", "reason": None, "auto": False, "checks": {"acct_requested": d(14)}}
        raw["steps"].insert(4, ins)
        raw["side_steps"] = []
        storage.save_case(raw)
    v = client.get(f"/api/cases/{c['id']}").json()
    assert "ops_acct_inspection" not in [s["id"] for s in v["steps"]]
    step = v["side_track"]["steps"][0]
    assert step["status"] == "done" and step["date"] == d(12) and v["side_track"]["done"]
