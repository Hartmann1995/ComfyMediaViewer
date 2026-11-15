@echo off
rem ================================================
rem  ComfyMediaViewer dev launcher (run from source, no PyInstaller)
rem  Python lookup order (override with CMV_PYTHON):
rem    %CMV_PYTHON% -> .venv / venv inside this folder -> python on PATH
rem  Extra args are passed through, e.g.:
rem    start_dev.bat --root "D:////Images"
rem    start_dev.bat --lan
rem  NOTE 1: keep this file ASCII-only - cmd.exe parses .bat in the ANSI codepage.
rem  NOTE 2: use goto instead of if(...) blocks around %PILTARGET% / %PY% -
rem          the project folder name may contain parentheses, and a bare ")"
rem          from %-expansion inside a parenthesized block aborts the script.
rem ================================================
setlocal
set "PY=%CMV_PYTHON%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY set "PY=python"

rem Pillow check: install into a project-local isolated dir if missing.
rem V2.12.1: default pypi times out on many CN networks - retry via TUNA mirror,
rem then verify the import and print a clear warning if it still failed.
set "PILTARGET=%~dp0.pil-fallback"
"%PY%" -c "import PIL" 2>nul
if not errorlevel 1 goto pil_ok
echo [start_dev] Pillow not found, installing into %PILTARGET% ...
"%PY%" -m pip install -q --target "%PILTARGET%" pillow
if not errorlevel 1 goto pil_have
echo [start_dev] default index failed, retrying via TUNA mirror ...
"%PY%" -m pip install -q --target "%PILTARGET%" -i https://pypi.tuna.tsinghua.edu.cn/simple pillow
:pil_have
"%PY%" -c "import PIL" 2>nul
if errorlevel 1 echo [start_dev] WARNING: Pillow install failed, thumbnails/dedup stay disabled.
set "PYTHONPATH=%PILTARGET%"
:pil_ok

rem NSFW engine check (onnxruntime + numpy): optional, same isolated-dir trick as Pillow.
rem Missing it only disables content detection - everything else keeps working.
rem goto instead of if(...): a literal ")" from %-expansion inside a parenthesized
rem block would abort the script (see NOTE 2 at the top).
set "NSFWTARGET=%~dp0.nsfw-fallback"
"%PY%" -c "import onnxruntime, numpy" 2>nul
if not errorlevel 1 goto nsfw_ok
echo [start_dev] NSFW engine not found, installing into %NSFWTARGET% ...
"%PY%" -m pip install -q --target "%NSFWTARGET%" onnxruntime numpy
if not errorlevel 1 goto nsfw_set
echo [start_dev] default index failed, retrying via TUNA mirror ...
"%PY%" -m pip install -q --target "%NSFWTARGET%" -i https://pypi.tuna.tsinghua.edu.cn/simple onnxruntime numpy
:nsfw_set
"%PY%" -c "import onnxruntime, numpy" 2>nul
if errorlevel 1 echo [start_dev] WARNING: NSFW engine install failed, content detection stays disabled.
if defined PYTHONPATH goto nsfw_join
set "PYTHONPATH=%NSFWTARGET%"
goto nsfw_ok
:nsfw_join
set "PYTHONPATH=%PYTHONPATH%;%NSFWTARGET%"
:nsfw_ok

title ComfyMediaViewer dev (source)
echo [start_dev] python = %PY%
echo [start_dev] starting http://127.0.0.1:8765/ ...
"%PY%" "%~dp0server.py" %*
echo.
echo [start_dev] server exited.
pause
