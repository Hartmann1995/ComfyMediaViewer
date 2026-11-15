@echo off
rem ================================================
rem  ComfyUI Media Viewer launcher
rem  Python lookup order (override with CMV_PYTHON):
rem    %CMV_PYTHON% -> portable python next to/above this script -> python on PATH
rem  Extra args are passed through, e.g.:
rem    start_viewer.bat --root "D:\Images" --port 8888
rem  NOTE: keep this file ASCII-only - cmd.exe parses .bat in the ANSI codepage,
rem        so non-ASCII comments can break command parsing.
rem ================================================
setlocal
set "PY=%CMV_PYTHON%"
if not defined PY if exist "%~dp0python_embeded\python.exe"    set "PY=%~dp0python_embeded\python.exe"
if not defined PY if exist "%~dp0python\python.exe"            set "PY=%~dp0python\python.exe"
if not defined PY if exist "%~dp0..\python_embeded\python.exe" set "PY=%~dp0..\python_embeded\python.exe"
if not defined PY if exist "%~dp0..\python\python.exe"        set "PY=%~dp0..\python\python.exe"
if not defined PY set "PY=python"
"%PY%" "%~dp0server.py" %*
if errorlevel 1 (
  echo.
  echo [ComfyMediaViewer] server failed to start.
  echo If Python was not found: install Python 3.10+ or set CMV_PYTHON to python.exe.
  echo The message above says what went wrong.
  pause
)
endlocal
