@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0ocr_cli.py" %*
) else (
    py -3.13 "%~dp0ocr_cli.py" %*
)
exit /b %errorlevel%
