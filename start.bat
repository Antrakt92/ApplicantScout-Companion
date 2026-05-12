@echo off
cd /d "%~dp0"
title ApplicantScout Companion

if not exist ".venv\Scripts\applicant-scout.exe" (
    echo ApplicantScout local environment is not installed.
    echo.
    echo From this folder, run:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e .[dev] -c constraints-release.txt
    echo.
    echo Then start this file again.
    echo.
    echo Press any key to close window.
    pause >nul
    exit /b 1
)

.venv\Scripts\applicant-scout.exe
echo.
echo Companion exited. Press any key to close window.
pause >nul
