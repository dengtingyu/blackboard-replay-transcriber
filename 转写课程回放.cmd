@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import av, certifi, faster_whisper, opencc, playwright; assert int(av.__version__.split('.')[0]) < 19" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -X utf8 transcribe.py %*
if errorlevel 1 goto failed
popd
exit /b 0

:failed
popd
pause
exit /b 1
