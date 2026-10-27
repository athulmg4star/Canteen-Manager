@echo off
cd /d "%~dp0"
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (echo Setup failed. Check the error above. & pause & exit /b 1)
echo Setup complete.
pause
