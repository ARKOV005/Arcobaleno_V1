ARCOBALENO WINDOWS LAUNCHER + ICON
=================================

WHY THERE WAS NO READY EXE
--------------------------
START_ARCOBALENO.bat already launches the source edition. A Windows .exe must
be compiled on Windows; PyInstaller does not cross-build a Windows executable
from the Linux workspace used to prepare the project.

A .bat file also cannot carry its own custom icon. Windows displays a custom
icon on an .exe or on a desktop shortcut (.lnk).

WHAT THIS PATCH ADDS
--------------------
- assets\ARCOBALENO.ico     Multi-resolution Windows icon
- assets\ARCOBALENO.png     Original icon artwork
- CREATE_ARCOBALENO_SHORTCUT.bat
- BUILD_WINDOWS_EXE.bat
- ARCOBALENO.spec
- requirements-build.txt
- windows_version_info.txt
- .github\workflows\build-windows.yml

OPTION 1: USE THE ICON NOW
--------------------------
Double-click CREATE_ARCOBALENO_SHORTCUT.bat.

It creates an ARCOBALENO shortcut on your Desktop. Until an EXE is built, the
shortcut launches START_ARCOBALENO.bat while displaying the custom icon.

OPTION 2: BUILD THE EXE ON YOUR LAPTOP
--------------------------------------
Double-click BUILD_WINDOWS_EXE.bat.

The first build downloads PyInstaller and may take several minutes. Output:

  dist\ARCOBALENO\ARCOBALENO.exe
  dist\ARCOBALENO-Windows-v1.1.0-preview1.zip

The result is a one-folder portable build. Keep the entire ARCOBALENO folder;
do not move only the EXE away from its _internal folder.

OPTION 3: BUILD THROUGH GITHUB
------------------------------
Commit and push these files. In GitHub:

1. Open the Actions tab.
2. Select "Build Windows EXE".
3. Click "Run workflow".
4. Open the completed run.
5. Download the ARCOBALENO-Windows artifact.

Future version tags beginning with v automatically trigger the same build.

FILES TO IGNORE IN GIT
----------------------
Add these lines to .gitignore if they are not already present:

  .build-venv/
  build/
  dist/
  *.exe

Keep ARCOBALENO.spec, requirements-build.txt, the assets folder, and the
.github workflow in GitHub.
