@echo off
rem Launch JARVIS from anywhere. Extra arguments are passed through, e.g. run.bat --text
cd /d "%~dp0"

rem Fastest path: the project's own environment, already installed by `uv sync`.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py %*
  exit /b %errorlevel%
)

rem No environment yet: let uv create it.
where uv >nul 2>nul && (uv run main.py %* & exit /b %errorlevel%)
if exist "%APPDATA%\Python\Python312\Scripts\uv.exe" (
  "%APPDATA%\Python\Python312\Scripts\uv.exe" run main.py %*
  exit /b %errorlevel%
)
echo uv not found and no .venv yet. Install uv with: python -m pip install --user uv
exit /b 1
