@echo off
setlocal
title ARCOBALENO
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto :try_python
py -3 launcher.py
set "ARC_EXIT=%ERRORLEVEL%"
goto :finished

:try_python
where python >nul 2>&1
if errorlevel 1 goto :python_missing
python launcher.py
set "ARC_EXIT=%ERRORLEVEL%"
goto :finished

:python_missing
echo.
echo [ARCOBALENO] Python was not found.
echo Install Python, enable "Add Python to PATH", then try again.
set "ARC_EXIT=1"

:finished
if not "%ARC_EXIT%"=="0" (
    echo.
    echo ARCOBALENO stopped because of an error.
    echo Check the message above or logs\bootstrap-errors.log.
    echo.
    pause
)

endlocal & exit /b %ARC_EXIT%
