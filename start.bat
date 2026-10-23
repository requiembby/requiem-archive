@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 goto useuv
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m requiem_archive
if errorlevel 1 goto fail
exit /b 0
:useuv
if not exist ".venv\Scripts\python.exe" (
  uv venv --python 3.11 .venv
  if errorlevel 1 goto fail
)
uv pip install --python .venv\Scripts\python.exe -e .
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m requiem_archive
if errorlevel 1 goto fail
exit /b 0
:fail
echo Install Python 3.11 or newer from python.org, with the Python launcher enabled.
pause
exit /b 1
