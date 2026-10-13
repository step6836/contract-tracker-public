@echo off
cd /d %~dp0
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate
pip install -q -r requirements.txt
if not exist data\users.json (
  echo No users yet. Let's create the first one.
  python -m backend.create_user
)
echo.
echo Contracts Tracker is starting. Open http://localhost:8000 on this computer,
echo or http://YOUR-IP-ADDRESS:8000 from another computer on the network.
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
