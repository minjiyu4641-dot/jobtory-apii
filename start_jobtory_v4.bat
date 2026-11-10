@echo off
setlocal
cd /d "%~dp0"
title Jobtory v4.3 - Financial Details First
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m streamlit run "%~dp0jobtory_v4.py" --server.port 8504
) else (
    python -m streamlit run "%~dp0jobtory_v4.py" --server.port 8504
)
pause
