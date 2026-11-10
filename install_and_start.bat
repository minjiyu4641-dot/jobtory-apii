@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m streamlit run "%~dp0jobtory_v4.py" --server.port 8504
pause
exit /b 0
:failed
echo Setup failed. Check Python and the message above.
pause
exit /b 1
