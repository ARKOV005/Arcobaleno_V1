@echo off
setlocal EnableExtensions
title Build ARCOBALENO Windows EXE
cd /d "%~dp0"

set "BUILD_VENV=.build-venv"
set "BUILD_PYTHON=%BUILD_VENV%\Scripts\python.exe"
set "OUTPUT_DIR=dist\ARCOBALENO"
set "OUTPUT_ZIP=dist\ARCOBALENO-Windows-v1.1.0-preview1.zip"

echo.
echo ================================================================
echo   ARCOBALENO Windows EXE Builder
echo ================================================================
echo.

if not exist "launcher.py" goto :wrong_folder
if not exist "ARCOBALENO.spec" goto :missing_files
if not exist "assets\ARCOBALENO.ico" goto :missing_files

if exist "%BUILD_PYTHON%" goto :environment_ready

where py >nul 2>&1
if not errorlevel 1 (
    echo [BUILD] Creating private build environment with Python Launcher...
    py -3 -m venv "%BUILD_VENV%"
    goto :check_environment
)

where python >nul 2>&1
if errorlevel 1 goto :python_missing

echo [BUILD] Creating private build environment with Python...
python -m venv "%BUILD_VENV%"

:check_environment
if not exist "%BUILD_PYTHON%" goto :build_failed

:environment_ready
echo [BUILD] Installing application and build dependencies...
"%BUILD_PYTHON%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :build_failed
"%BUILD_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-build.txt
if errorlevel 1 goto :build_failed

echo [BUILD] Removing old build output...
if exist "build" rmdir /s /q "build"
if exist "%OUTPUT_DIR%" rmdir /s /q "%OUTPUT_DIR%"
if exist "%OUTPUT_ZIP%" del /q "%OUTPUT_ZIP%"

echo [BUILD] Compiling ARCOBALENO.exe...
"%BUILD_PYTHON%" -m PyInstaller --clean --noconfirm ARCOBALENO.spec
if errorlevel 1 goto :build_failed

if not exist "%OUTPUT_DIR%\ARCOBALENO.exe" goto :build_failed

if exist "config.example.json" (
    copy /y "config.example.json" "%OUTPUT_DIR%\config.json" >nul
) else if exist "config.json" (
    copy /y "config.json" "%OUTPUT_DIR%\config.json" >nul
)

if exist "LICENSE.txt" copy /y "LICENSE.txt" "%OUTPUT_DIR%\LICENSE.txt" >nul
if exist "OPERATOR_README.txt" copy /y "OPERATOR_README.txt" "%OUTPUT_DIR%\OPERATOR_README.txt" >nul
copy /y "assets\ARCOBALENO.ico" "%OUTPUT_DIR%\ARCOBALENO.ico" >nul
copy /y "assets\ARCOBALENO.png" "%OUTPUT_DIR%\ARCOBALENO.png" >nul

type nul > "%OUTPUT_DIR%\.first_run_pending"

(
    echo @echo off
    echo cd /d "%%~dp0"
    echo ARCOBALENO.exe
    echo if errorlevel 1 pause
) > "%OUTPUT_DIR%\START_ARCOBALENO.bat"

echo [BUILD] Creating portable ZIP...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%OUTPUT_DIR%\*' -DestinationPath '%OUTPUT_ZIP%' -Force"
if errorlevel 1 goto :build_failed

echo.
echo [BUILD] SUCCESS
 echo EXE folder: %CD%\%OUTPUT_DIR%
 echo ZIP file  : %CD%\%OUTPUT_ZIP%
echo.
echo You can run: %OUTPUT_DIR%\ARCOBALENO.exe
pause
exit /b 0

:wrong_folder
echo [BUILD] launcher.py was not found.
echo Extract these files into the ARCOBALENO project root, then try again.
goto :failed_exit

:missing_files
echo [BUILD] One or more Windows build files are missing.
echo Re-extract the Windows launcher patch into the project root.
goto :failed_exit

:python_missing
echo [BUILD] Python was not found.
echo Install Python and enable "Add Python to PATH", then try again.
goto :failed_exit

:build_failed
echo.
echo [BUILD] The Windows EXE build failed.
echo Review the error above. The source version was not changed.

:failed_exit
echo.
pause
exit /b 1
