@echo off
REM ============================================================
REM  Build jade.tools into a folder (onedir, no console window).
REM  Result: dist\jade.tools\jade.tools.exe
REM
REM  NOTE: keep this file ASCII-only (no Cyrillic) and do NOT add
REM  "chcp 65001" here - that combination is a known cmd.exe bug:
REM  it garbles line parsing mid-script (multi-byte UTF-8 chars get
REM  cut into fragments that cmd then tries to run as commands).
REM  The app's own UI stays Russian - this only affects the build
REM  script's own console messages.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] Checking dependencies...
py -c "import flask, pystray, PIL, PyInstaller" 2>nul
if errorlevel 1 (
    echo Missing dependencies. Installing...
    py -m pip install -r requirements.txt pystray pyinstaller
)

echo [2/4] Generating icon (if missing)...
if not exist logo.ico py make_icon.py

REM PyInstaller (--clean, onedir) fully re-creates dist\jade.tools\ on every
REM build - not just build output, but anything the APP itself put there at
REM runtime too. settings.json (incl. local paths/cookies path), bin\, temp\,
REM cache\, logs\, cookies.txt all live in that same folder and would be
REM silently wiped on every rebuild otherwise. Back them up first, restore
REM them after the build succeeds (below). First-ever build - no
REM dist\jade.tools\settings.json yet - just skips both steps, no error.
set "APP_DIR=dist\jade.tools"
set "BUILD_BACKUP=%TEMP%\jade_build_backup"
set "HAVE_BACKUP=0"

echo [3/4] Backing up runtime state from a previous build (if any)...
if exist "%APP_DIR%\settings.json" (
    if exist "%BUILD_BACKUP%" rmdir /s /q "%BUILD_BACKUP%"
    mkdir "%BUILD_BACKUP%"
    copy /y "%APP_DIR%\settings.json" "%BUILD_BACKUP%\settings.json" >nul
    if exist "%APP_DIR%\cookies.txt" copy /y "%APP_DIR%\cookies.txt" "%BUILD_BACKUP%\cookies.txt" >nul
    for %%D in (bin temp cache logs) do (
        if exist "%APP_DIR%\%%D" xcopy "%APP_DIR%\%%D" "%BUILD_BACKUP%\%%D\" /E /I /Q /Y >nul
    )
    set "HAVE_BACKUP=1"
) else (
    echo   Nothing to back up - first build.
)

echo [4/4] Building exe with PyInstaller...
py -m PyInstaller --noconfirm --clean jade.spec
if errorlevel 1 (
    echo.
    echo Build failed - see the error above.
    pause
    exit /b 1
)

REM readme.txt must sit next to the exe on disk, not inside the PyInstaller
REM bundle (jade.spec/datas) - a bundled file extracts into the temporary
REM _MEIPASS folder, not next to the exe (same sys.executable vs __file__
REM issue as the app's own portable paths, see CLAUDE.md).
if exist readme.txt copy /y readme.txt dist\jade.tools\readme.txt >nul

REM Restore the runtime state backed up above, on top of the fresh build.
if "%HAVE_BACKUP%"=="1" (
    echo Restoring runtime state from the previous build...
    copy /y "%BUILD_BACKUP%\settings.json" "%APP_DIR%\settings.json" >nul
    if exist "%BUILD_BACKUP%\cookies.txt" copy /y "%BUILD_BACKUP%\cookies.txt" "%APP_DIR%\cookies.txt" >nul
    for %%D in (bin temp cache logs) do (
        if exist "%BUILD_BACKUP%\%%D" xcopy "%BUILD_BACKUP%\%%D" "%APP_DIR%\%%D\" /E /I /Q /Y >nul
    )
    rmdir /s /q "%BUILD_BACKUP%"
)

echo.
echo ============================================
echo  Done: dist\jade.tools\jade.tools.exe
echo  Move the whole dist\jade.tools folder where you want it,
echo  then put jade.tools.exe into autostart if you want.
echo ============================================
endlocal
pause
