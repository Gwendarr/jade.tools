# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec для сборки jade.tools в папку (onedir).

Особенности:
  * onedir — exe + папка _internal/ рядом (не onefile: самораспаковка
    onefile-сборки во %TEMP% — типичный триггер ложных срабатываний
    антивирусов на PyInstaller-бинарники, см. ARCHITECTURE.md);
  * windowed (noconsole) — без чёрного окна консоли (GUI-приложение в трее);
  * упакованы templates/ и logo.ico (нужны Flask-у приложению и трею);
  * иконка exe = logo.ico.

Сборка:
    pyinstaller jade.spec
Результат: dist/jade.tools/jade.tools.exe (+ dist/jade.tools/_internal/)
"""

block_cipher = None

a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),   # папка с index.html
        ('logo.ico', '.'),            # иконка для трея
    ],
    hiddenimports=[
        # pystray на Windows использует win32-бэкенд.
        'pystray._win32',
        'PIL._tkinter_finder',
        # Запасные бэкенды трея (на случай нестандартной среды).
        'pywintypes',
        # tkinter импортируется лениво (диалоги выбора пути в настройках) —
        # подсказываем PyInstaller включить его явно.
        'tkinter', 'tkinter.filedialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Отрезаем тяжёлые и ненужные модули для уменьшения размера.
        # tkinter НЕ исключаем — нужен для нативных диалогов выбора пути
        # на странице настроек.
        'unittest', 'pydoc', 'doctest',
        'pytest', 'IPython', 'matplotlib', 'numpy', 'pandas',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='jade.tools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX часто роняет антивирусы; выключен.
    console=False,          # windowed: без консольного окна.
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='jade.tools',
)
