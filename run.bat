@echo off
rem Launch JARVIS from anywhere. Extra arguments are passed through, e.g. run.bat --text
cd /d "%~dp0"
set "UV=uv"
where uv >nul 2>nul || set "UV=%APPDATA%\Python\Python312\Scripts\uv.exe"
if not "%UV%"=="uv" if not exist "%UV%" (
  echo uv not found. Install it with: python -m pip install --user uv
  exit /b 1
)
"%UV%" run main.py %*
