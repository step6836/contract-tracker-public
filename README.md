# Contracts Tracker

Local tool for the contracts team to track Renewals, Transfers, Expansions, and Leases step by step.
Data is plain JSON on disk. Nothing leaves your network.

## Run it (Windows)

1. Double-click `start.bat`. The first run creates a virtual environment, installs the Python
   packages, and asks you to create the first user.
2. Open `http://localhost:8000`. Teammates use `http://<your-ip>:8000`
   (find your IP with `ipconfig`, the "IPv4 Address" line).
3. Add the second user any time: `python -m backend.create_user` (from an activated `.venv`).
   Running it again with an existing username resets that person's password.

Other computers can only connect if the port is open. In an **admin** Command Prompt:

    netsh advfirewall firewall add rule name="Contracts Tracker" dir=in action=allow protocol=TCP localport=8000

The app is only reachable while the host computer is on and `start.bat` is running.
Give that computer a reserved IP in your router so the address doesn't change.

## Try it with fake data

    python -m backend.seed_demo

Adds five made-up "Demo Store" cases. To start fresh afterwards, delete the `data\cases` folder.

## Where things live

| Path | What it is |
| --- | --- |
| `backend/templates/*.json` | The four workflows (steps, order, owners, review periods). Edit these to change the process. |
| `data/cases/<id>.json` | One file per case (current state, step dates, notes). |
| `data/events.jsonl` | Append-only log: who did what, and when. |
| `data/users.json` | Users (passwords are hashed). |
| `data/config.json` | Optional. `{"default_stale_days": 7}` sets how long a step can sit before it's flagged. |

Back up the `data` folder (it holds franchisee names and case notes). It is git-ignored on purpose.

## Exec report in Box

The tracker can keep a read-only Excel status report in a Box Drive folder, so execs can see
where every case stands without an account. Add the full path of the report file to `data/config.json`:

    {"default_stale_days": 7, "publish_path": "C:/Users/you/Box/Reports/Contracts Status.xlsx"}

- The folder must already exist; the file is created on the first publish.
- The report is rebuilt from scratch when the app starts, about 10 seconds after any case change,
  and whenever someone clicks **Publish now** on the All cases page. Never edit the Excel file itself:
  the next publish replaces it.
- In Box, give execs **Viewer** access to the folder so they can open but not change it.
- If the file can't be written (Box Drive not running, file open in Excel on this computer), the
  tracker keeps working and logs the error; the next publish tries again.
- Without `publish_path`, publishing is off.

## How steps behave

- Steps go strictly in order. The first step is done on the case's start date.
- Steps with a review period (FDD & NDA 14 days, FA Draft 7 days) show when the review ends.
  They flag as stale 3 days after that; every other step uses `default_stale_days`.
- For Expansion, the lease runs on its own track and can finish before or after signing.
- For Renewal, the Operations & Accounting Team Inspection runs on its own track, alongside FDD & NDA and
  everything after it. It opens once the Contracts Team Evaluation (and Management Confirmation, if needed)
  is done, and it has to be satisfactory before the signing can be scheduled. Its day count and stale flag
  work like a main step's.
- Steps that a template moves from one track to the other keep their history on existing cases.
- Template changes apply to existing cases: new steps appear as pending, and finished steps keep their dates.

## Lease cases and links

A Lease case is for a premises whose lease is expiring: the follow-up goes out 6 months before it expires,
the lease draft comes in (extension, renewal, new lease or assignment), and it's reviewed until it's
approved. It's flagged 60 days before the lease expires and again once it has expired.

A lease case can be linked to any open renewal, transfer or expansion (on the case page, or when the lease
case is created). Both cases show the link, where the other case stands, and who opened it, so whoever
handles each one is in the loop. If a lease case and another open case share a store # but aren't linked,
both show up in Needs attention until someone links them. A renewal, transfer or expansion page also has
**Start a lease case for this store**, which fills in the store and the link.

## Changing the frontend

The built UI is in `frontend/dist`, so you don't need Node to run the app. To modify it:

    cd frontend
    npm install
    npm run dev        (UI on :5173, proxies to the API on :8000)
    npm run build      (writes frontend/dist)

## Tests

    python -m pip install pytest httpx
    python -m pytest tests -q
