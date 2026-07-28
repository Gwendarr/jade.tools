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

echo [1/3] Checking dependencies...
py -c "import flask, pystray, PIL, PyInstaller" 2>nul
if errorlevel 1 (
    echo Missing dependencies. Installing...
    py -m pip install -r requirements.txt pystray pyinstaller
)

echo [2/3] Generating icon (if missing)...
if not exist logo.ico py make_icon.py

echo [3/3] Building exe with PyInstaller...
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

echo.
echo ============================================
echo  Done: dist\jade.tools\jade.tools.exe
echo  Move the whole dist\jade.tools folder where you want it,
echo  then put jade.tools.exe into autostart if you want.
echo ============================================
endlocal
pause
