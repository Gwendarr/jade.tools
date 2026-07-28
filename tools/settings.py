# -*- coding: utf-8 -*-
"""Страница настроек приложения.

Настройки хранятся в settings.json (см. core.save_settings/apply_settings) и
применяются ко всем инструментам через core. Здесь — роуты страницы и API:
чтение/запись (автосохранение), сброс, очистка кэша/куки/временных файлов,
нативные диалоги выбора пути/файла.
"""

import os
import threading
import subprocess
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify

import core

bp = Blueprint("settings", __name__, url_prefix="/settings")

# =============================================================================
#  ВЕРСИЯ ПРИЛОЖЕНИЯ — обновлять вручную при релизах.
#  Формат: "x.ddmmyy" (x — номер релиза на GitHub; ddmmyy — дата изменений
#  и/или публикации: день/месяц/год, год 2 цифры)
#  Значение статичное (НЕ вычисляется от системной даты).
# =============================================================================
VERSION_NUMBER = "0.280726"
VERSION = f"{VERSION_NUMBER} - jade.tools by Gwendarr"

# Метаданные для навигации (иконка-шестерёнка добавляется отдельно, внизу панели).
TOOL = {"id": "settings", "name": "настройки", "url": "/settings/"}


def _stats():
    """Статистика по кэшу / временным файлам / куки / логу (для раздела «файлы»)."""
    cookies = core.resolve_cookies_file()
    return {
        "temp": core.path_stats(core.DOWNLOADS_DIR),
        "cache": core.path_stats(core.CACHE_DIR),
        "cookies": (core.path_stats(cookies) if cookies
                    else {"exists": False, "size": 0, "mtime": None, "atime": None}),
        "log": core.path_stats(core.LOG_FILE),
    }


def _validate(key, value):
    """Проверка значения настройки. Возвращает текст ошибки или None.

    Тема — особый случай: неверное значение не отклоняется ошибкой (см.
    api_save — там оно тихо приводится к "dark"), чтобы не ронять сохранение
    остальных полей из-за мусора в одном селекте."""
    if key in ("temp_custom_path", "cache_custom_path"):
        v = (value or "").strip()
        if v and not os.path.isdir(v):
            return "Папка не существует — укажите существующую директорию."
    if key == "cookies_file_path":
        v = (value or "").strip()
        if v:
            if not os.path.isfile(v):
                return "Файл не найден."
            if not v.lower().endswith(".txt"):
                return "Файл куки должен быть в формате .txt (Netscape)."
    return None


def _path_row(mode, custom_path, app_fn, system_fn, available, configured_path):
    """Данные одной строки (temp или cache) для трёхрежимного UI: реальный
    путь-кандидат для КАЖДОГО режима (чтобы поле пути показывало актуальный
    путь сразу при переключении дропдауна, без похода на сервер) + текущий
    статус доступности настроенного режима."""
    return {
        "mode": mode, "custom_path": custom_path or "",
        "app_path": str(app_fn()), "system_path": str(system_fn()),
        "available": available, "configured_path": str(configured_path),
    }


def _paths():
    s = core.get_settings()
    return {
        "temp": _path_row(s["temp_mode"], s["temp_custom_path"],
                          core.default_temp_dir, core.system_temp_dir,
                          core.TEMP_AVAILABLE, core.TEMP_CONFIGURED_PATH),
        "cache": _path_row(s["cache_mode"], s["cache_custom_path"],
                           core.default_cache_dir, core.system_cache_dir,
                           core.CACHE_AVAILABLE, core.CACHE_CONFIGURED_PATH),
    }


def _state():
    return {
        "settings": core.get_settings(),
        "defaults": core.DEFAULT_SETTINGS,
        "version": VERSION,
        "default_temp": str(core.default_temp_dir()),
        "default_cache": str(core.default_cache_dir()),
        "stats": _stats(),
        "deps": {"groups": core.dependency_groups(),
                 "ytdlp_source_kind": core.ytdlp_source_kind()},
        "paths": _paths(),
    }


# --- Роуты -------------------------------------------------------------------

@bp.route("/")
def page():
    return render_template("settings.html")


@bp.route("/api/state")
def api_state():
    return jsonify(_state())


@bp.route("/api/save", methods=["POST"])
def api_save():
    """Автосохранение: принимает частичные изменения, валидирует, применяет."""
    data = request.get_json(silent=True) or {}
    for k, v in data.items():
        if k not in core.DEFAULT_SETTINGS:
            continue
        err = _validate(k, v)
        if err:
            return jsonify({"error": err, "field": k}), 400
    if "theme" in data and data["theme"] not in ("dark", "light"):
        data["theme"] = "dark"
    core.save_settings(data)
    return jsonify({"ok": True, "settings": core.get_settings(), "stats": _stats(),
                    "paths": _paths()})


@bp.route("/api/reset", methods=["POST"])
def api_reset():
    core.reset_settings()
    return jsonify({"ok": True, **_state()})


@bp.route("/api/clear", methods=["POST"])
def api_clear():
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if target == "cache":
        core.clear_path(core.CACHE_DIR)
    elif target == "temp":
        core.clear_path(core.DOWNLOADS_DIR)
    else:
        return jsonify({"error": "Неизвестная категория очистки."}), 400
    return jsonify({"ok": True, "stats": _stats()})


def _pick(kind):
    """Нативный диалог выбора папки/файла (tkinter в отдельном потоке)."""
    result = {}

    def run():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if kind == "dir":
                result["path"] = filedialog.askdirectory()
            else:
                result["path"] = filedialog.askopenfilename(
                    filetypes=[("Файлы куки", "*.txt"), ("Все файлы", "*.*")])
            root.destroy()
        except Exception as e:
            result["error"] = str(e)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=180)
    return result.get("path") or "", result.get("error")


@bp.route("/api/pick", methods=["POST"])
def api_pick():
    data = request.get_json(silent=True) or {}
    kind = "dir" if (data.get("kind") or "dir") == "dir" else "file"
    path, err = _pick(kind)
    if err:
        return jsonify({"error": "Не удалось открыть диалог выбора: " + err}), 500
    return jsonify({"ok": True, "path": path})


@bp.route("/api/open_path", methods=["POST"])
def api_open_path():
    """Кнопка «Открыть» у строк temp/cache в режимах «в папке приложения»/
    «в системе» (см. этап 4) — создаёт папку (temp/cache создаются лениво,
    как и везде в приложении) и открывает её в проводнике. Для режима
    «свой путь» кнопка на странице показывает «Обзор» и дёргает /api/pick,
    не этот роут.

    target="cookies" — кнопка «показать» у куки в разделе «файлы» (см.
    core.resolve_cookies_file()): в отличие от temp/cache это конкретный
    ФАЙЛ, а не папка — открывает проводник с выделенным файлом (тот же
    паттерн, что у /api/reveal_log), ничего не создаёт. Если файла нет —
    понятная ошибка, без попытки открыть проводник в пустоту."""
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if target == "cookies":
        cookies_path = core.resolve_cookies_file()
        if not cookies_path:
            return jsonify({"error": "Файл cookies.txt не найден."}), 404
        try:
            subprocess.Popen(["explorer", "/select," + cookies_path])
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": "Не удалось открыть проводник: " + str(e)}), 500
    path = {"temp": core.TEMP_CONFIGURED_PATH,
            "cache": core.CACHE_CONFIGURED_PATH}.get(target)
    if path is None:
        return jsonify({"error": "Неизвестная папка."}), 400
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(path)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Не удалось открыть папку: " + str(e)}), 500


@bp.route("/api/reveal_log", methods=["POST"])
def api_reveal_log():
    """Открыть системный проводник с выделенным файлом логов (не открывать файл
    внутри приложения — только показать его местоположение)."""
    try:
        if core.LOG_FILE.is_file():
            # /select, склеен с путём в один аргумент — так его понимает explorer.
            subprocess.Popen(["explorer", "/select," + str(core.LOG_FILE)])
        else:
            core.LOG_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(core.LOG_DIR)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Не удалось открыть проводник: " + str(e)}), 500


@bp.route("/api/check_ytdlp", methods=["POST"])
def api_check_ytdlp():
    """Ручная проверка обновления yt-dlp (в обход кэша — всегда свежий запрос)."""
    return jsonify(core.check_ytdlp_update(force=True))
