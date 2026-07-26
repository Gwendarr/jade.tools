# -*- coding: utf-8 -*-
"""
YouTube Downloader — системный трей.

Запускает встроенный Flask-сервер (app.py) в фоновом потоке и показывает
иконку в области уведомлений. Меню трея:
    • Открыть в браузере  — открывает http://127.0.0.1:5000
    • Папка программы      — открывает папку, где лежит exe
    • Выход               — останавливает сервер и закрывает приложение

После сборки PyInstaller-ом получается один exe без консольного окна,
который можно положить в автозапуск.
"""

import os
import sys
import time
import socket
import threading
import webbrowser
import subprocess

from PIL import Image

# Флаг скрытия консольного окна дочерних процессов (tasklist/taskkill) на Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# --- Определение путей (важно для собранного .exe) --------------------------

def _resource_dir():
    """Папка, где лежат ресурсы (app.py, templates/, logo.ico).

    При запуске из исходников это папка скрипта.
    В собранном onefile-exe PyInstaller распаковывает ресурсы во временный
    каталог _MEIPASS — туда и смотрим.
    """
    if getattr(sys, "frozen", False):
        # onefile: ресурсы в _MEIPASS; постоянные данные (downloads) — рядом с exe.
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _app_dir():
    """Папка для пользовательских данных (downloads/, логи).

    Рядом с exe в собранном виде; рядом со скриптом — в исходниках.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


RESOURCE_DIR = _resource_dir()
APP_DIR = _app_dir()

HOST = os.environ.get("YTD_HOST", "127.0.0.1")
PORT = int(os.environ.get("YTD_PORT", "5000"))
URL = f"http://{HOST}:{PORT}"


# --- Один экземпляр ---------------------------------------------------------

def _kill_other_instances():
    """Завершает другие запущенные экземпляры приложения.

    При повторном запуске exe старый процесс закрывается, чтобы в трее всегда
    оставался ровно один экземпляр. Работает для собранного exe (по имени
    образа); из исходников single-instance не форсируется."""
    if not getattr(sys, "frozen", False):
        return
    me = os.getpid()
    image = os.path.basename(sys.executable)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
    except Exception:
        return
    for line in out.stdout.splitlines():
        cols = [c.strip().strip('"') for c in line.split(",")]
        if len(cols) < 2:
            continue
        try:
            pid = int(cols[1])
        except ValueError:
            continue
        if pid and pid != me:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=_NO_WINDOW)
            except Exception:
                pass


def _wait_port_free(host, port, timeout=4.0):
    """Ждёт освобождения порта после завершения прошлого экземпляра."""
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            time.sleep(0.2)   # кто-то ещё слушает — подождём
        except OSError:
            s.close()
            return            # порт свободен
        finally:
            try:
                s.close()
            except Exception:
                pass


# --- Запуск сервера ---------------------------------------------------------

_server_thread = None


def _start_server():
    """Запускает Flask-сервер из app.py в фоновом потоке."""
    global _server_thread
    _server_thread = threading.Thread(target=_run_flask, daemon=True)
    _server_thread.start()


def _run_flask():
    """Импортирует app.py и запускает werkzeug-сервер без вывода в консоль."""
    import logging

    # Перекладываем путь к ресурсам так, чтобы Flask нашёл templates/.
    sys.path.insert(0, RESOURCE_DIR)
    if RESOURCE_DIR != os.getcwd():
        os.chdir(RESOURCE_DIR)

    # Глушим лишний лог Werkzeug (у нас и так нет консоли).
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    import app as flask_app  # noqa: E402

    # Каталог загрузок core определяет сам (рядом с exe в собранном виде),
    # отдельно переопределять не нужно.
    try:
        flask_app.app.run(host=HOST, port=PORT, debug=False,
                          threaded=True, use_reloader=False)
    except Exception as e:
        # В onefile без консоли — хотя бы запишем ошибку в файл.
        try:
            with open(os.path.join(APP_DIR, "error.log"), "w", encoding="utf-8") as f:
                import traceback
                f.write(traceback.format_exc())
        except Exception:
            pass


# --- Трей -------------------------------------------------------------------

def _load_icon():
    """Загружает logo.ico; если нет — рисует простую иконку в памяти."""
    ico_path = os.path.join(RESOURCE_DIR, "logo.ico")
    try:
        return Image.open(ico_path)
    except Exception:
        # Резервная иконка: красный круг.
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.ellipse([6, 6, 58, 58], fill=(255, 59, 59, 255))
        return img


def _open_browser():
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def _open_app_folder():
    """Открывает папку программы — там, где лежит exe (APP_DIR)."""
    try:
        os.startfile(APP_DIR)  # Windows
    except Exception:
        try:
            subprocess.Popen(["explorer", APP_DIR])
        except Exception:
            pass


def _quit(icon, item):
    """Останавливает трей и закрывает приложение (сервер — daemon-поток)."""
    icon.stop()
    try:
        import core
        core.cleanup_all_jobs()
    except Exception:
        pass
    os._exit(0)


def _build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("🌐 Открыть в браузере", lambda i: _open_browser(),
                         default=True),
        pystray.MenuItem("📁 Папка программы", lambda i: _open_app_folder()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Выход", _quit),
    )


def main():
    import pystray

    # Флаг --no-browser отключает авто-открытие вкладки при старте.
    auto_browser = "--no-browser" not in sys.argv

    # Один экземпляр: закрываем прошлую копию и ждём, пока освободится порт.
    _kill_other_instances()
    _wait_port_free(HOST, PORT)

    # Запускаем сервер до создания иконки, чтобы к моменту клика он уже слушал.
    _start_server()

    # Автоматически открываем браузер через 1.5с после старта — чтобы сервер
    # успел подняться. В отдельном потоке, иначе заблокирует трей.
    if auto_browser:
        def _delayed_browser():
            import time
            time.sleep(1.5)
            _open_browser()
        threading.Thread(target=_delayed_browser, daemon=True).start()

    icon = pystray.Icon(
        name="jade.tools",
        icon=_load_icon(),
        title=f"jade.tools — {URL}",
        menu=_build_menu(),
    )
    # Блокирующий цикл трея (до вызова icon.stop() из меню «Выход»).
    icon.run()


if __name__ == "__main__":
    main()
