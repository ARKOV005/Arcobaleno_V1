@echo off
setlocal EnableExtensions
title Create ARCOBALENO Desktop Shortcut
cd /d "%~dp0"

set "ARC_ROOT=%CD%"
set "ARC_ICON=%CD%\assets\ARCOBALENO.ico"
set "ARC_TARGET=%CD%\START_ARCOBALENO.bat"

if exist "%CD%\ARCOBALENO.exe" set "ARC_TARGET=%CD%\ARCOBALENO.exe"
if exist "%CD%\dist\ARCOBALENO\ARCOBALENO.exe" (
    set "ARC_TARGET=%CD%\dist\ARCOBALENO\ARCOBALENO.exe"
    set "ARC_ROOT=%CD%\dist\ARCOBALENO"
)

if not exist "%ARC_ICON%" (
    echo ARCOBALENO.ico was not found in the assets folder.
    pause
    exit /b 1
)

if not exist "%ARC_TARGET%" (
    echo No ARCOBALENO launcher was found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop=[Environment]::GetFolderPath('Desktop');" ^
  "$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'ARCOBALENO.lnk'));" ^
  "$shortcut.TargetPath=$env:ARC_TARGET;" ^
  "$shortcut.WorkingDirectory=$env:ARC_ROOT;" ^
  "$shortcut.IconLocation=($env:ARC_ICON + ',0');" ^
  "$shortcut.Description='ARCOBALENO Portable Event Photo Experience';" ^
  "$shortcut.Save()"

if errorlevel 1 (
    echo Could not create the desktop shortcut.
    pause
    exit /b 1
)

echo.
echo ARCOBALENO shortcut created on your Desktop.
echo The shortcut uses your selected four-color icon.
echo.
pause
exit /b 0
