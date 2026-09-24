# -*- coding: utf-8 -*-
"""Общая инфраструктура для всех инструментов.

Здесь живёт то, что переиспользуют разные тулзы:
  * система фоновых задач (JOBS) с прогрессом и отменой;
  * вызовы yt-dlp (метаданные, базовые флаги);
  * вызовы ffmpeg/ffprobe (длительность, прогон с прогрессом);
  * путь к папке загрузок (рядом с exe в собранном виде).
"""

import os
import re
import sys
import time
import json
import uuid
import shutil
import hashlib
import logging
import tempfile
import zipfile
import datetime
import threading
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from logging.handlers import RotatingFileHandler

# --- Пути --------------------------------------------------------------------
# Папка пользовательских данных. В собранном onefile-exe — рядом с exe (а не во
# временном _MEIPASS), в исходниках — рядом со скриптами.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent


def _is_temp_launch():
    """True, если BASE_DIR лежит внутри временной папки Windows (%TEMP%/%TMP%)
    — типичный случай запуска exe прямо из архива без распаковки. Только для
    собранного exe: при запуске из исходников BASE_DIR — папка с кодом, не
    временная (см. onedir_and_startup_checks.md, часть 2)."""
    if not getattr(sys, "frozen", False):
        return False
    base = os.path.normcase(os.path.normpath(str(BASE_DIR)))
    for var in ("TEMP", "TMP"):
        raw = os.environ.get(var)
        if not raw:
            continue
        temp_root = os.path.normcase(os.path.normpath(raw))
        if base == temp_root or base.startswith(temp_root + os.sep):
            return True
    return False


RUNNING_FROM_TEMP = _is_temp_launch()


def default_bin_dir():
    """Папка внешних бинарников (yt-dlp/ffmpeg/ffprobe/deno), ПЛОСКО, рядом с
    exe — первый шаг резолвинга каждой зависимости (см. resolve_dependency)."""
    d = BASE_DIR / "bin"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


BIN_DIR = default_bin_dir()

# --- Логирование --------------------------------------------------------------
# Лог работы приложения — рядом с exe (портабельность), отдельно от temp/cache
# — чтобы очистка этих папок (см. страницу настроек) не задевала лог. Ротация:
# до 3 файлов по 2 МБ.
def default_log_dir():
    """Папка логов приложения — рядом с exe; фоллбэк на системную папку логов,
    если рядом с exe нет прав на запись (например, exe в Program Files)."""
    d = BASE_DIR / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        d = Path(base) / "jade.tools" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


LOG_DIR = default_log_dir()
LOG_FILE = LOG_DIR / "jade.log"

logger = logging.getLogger("jade")
logger.setLevel(logging.INFO)
if not logger.handlers:      # защита от повторной настройки при повторном импорте
    _handler = RotatingFileHandler(str(LOG_FILE), maxBytes=2 * 1024 * 1024,
                                   backupCount=3, encoding="utf-8")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)

logger.info("=" * 60)
logger.info("jade.tools: логирование запущено (pid=%s, файл=%s)", os.getpid(), LOG_FILE)

# --- Настройки приложения ----------------------------------------------------
# Все настройки хранятся в settings.json рядом с приложением и применяются к
# рабочим путям/параметрам функцией apply_settings(). Подробности — страница
# настроек (tools/settings.py).
SETTINGS_FILE = BASE_DIR / "settings.json"


# --- Версия приложения: разбор и сравнение -----------------------------------
# Формат: "x.ddmmyy" (x — номер релиза на GitHub, ddmmyy — день/месяц/две
# последние цифры года; например "1.220726"), опционально с префиксом v/V
# (так проставляются теги на GitHub). Одна функция для обеих задач ниже:
# отслеживания смены версии между запусками и сверки с последним релизом на
# GitHub — обеим нужно одно и то же: распарсить строку в сравнимую величину и
# ни в коем случае не упасть на кривом значении.
#
# Хотфиксы (x.ddmmyy-n) сейчас НЕ поддерживаются. Когда понадобятся —
# добавлять только здесь: доп. группу в _VERSION_RE и третий элемент кортежа
# в parse_version(); compare_versions() ниже сравнивает кортежи целиком и сам
# учтёт новый компонент, её трогать не придётся.
_VERSION_RE = re.compile(r'^[vV]?(\d+)\.(\d{2})(\d{2})(\d{2})$')


def parse_version(value):
    """Разбирает строку версии "x.ddmmyy" (опц. префикс v/V, пробелы по краям
    игнорируются) в (release:int, released:datetime.date) для сравнения.

    Возвращает None, если строка не соответствует формату ИЛИ дата не
    существует (невалидные день/месяц, например "32.999999") — разбор сам
    валидирует значение, посимвольно/полями строки не сравниваем. Ошибка
    разбора никогда не бросается наружу — вызывающий код проверяет None."""
    if not value:
        return None
    m = _VERSION_RE.match(value.strip())
    if not m:
        return None
    release_s, dd, mm, yy = m.groups()
    try:
        released = datetime.date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return (int(release_s), released)


def compare_versions(a, b):
    """Сравнивает версии a и b (строки "x.ddmmyy") через parse_version():
    сначала по релизу (x), при равенстве — по дате.

    Возвращает -1 (a < b) / 0 (a == b) / 1 (a > b), либо None, если хотя бы
    одна строка не распарсилась — "сравнение невозможно"; вызывающий код
    обязан трактовать None как "изменений/обновления нет", не как ошибку."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# --- Пути temp/cache: три режима (app|system|custom) --------------------------
# Чистые резолверы пути — БЕЗ побочных эффектов (не создают папку!). Кто хочет
# папку реально на диске — создаёт сам (см. apply_settings() и _dir_available()
# ниже, которой обязательно нужен путь ДО создания, чтобы проверить доступность).

def default_temp_dir():
    """Путь папки временных файлов в режиме «в папке приложения» (BASE_DIR/temp)."""
    return BASE_DIR / "temp"


def default_cache_dir():
    """Путь папки кэша в режиме «в папке приложения» (BASE_DIR/cache)."""
    return BASE_DIR / "cache"


def system_temp_dir():
    """Путь папки временных файлов в режиме «в системе» (системный TEMP)."""
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(base) / "jade.tools"


def system_cache_dir():
    """Путь папки кэша в режиме «в системе» (%LOCALAPPDATA%)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    return Path(base) / "jade.tools" / "cache"


def _dir_available(path):
    """True, если по этому пути можно писать: сама папка уже существует и
    доступна для записи, либо ближайший существующий предок доступен для
    записи (папка будет создана лениво). Ничего не создаёт — только проверяет.
    False, если добрались до несуществующего корня диска (диск отключён) или
    наткнулись на ошибку доступа."""
    try:
        p = Path(path)
        cur = p
        while not cur.exists():
            parent = cur.parent
            if parent == cur:      # дошли до корня — и того нет (диск отключён)
                return False
            cur = parent
        return os.access(str(cur), os.W_OK)
    except Exception:
        return False


DEFAULT_SETTINGS = {
    # Раздел 0 — приложение (блок в settings.html НАД «предпочтения скачивания»)
    "check_app_updates": True,      # фоновая проверка новых релизов на GitHub
                                     # (см. check_app_update/start_app_update_check);
                                     # выкл — проверка вообще не выполняется, не
                                     # только скрывает плашку. НЕ влияет на
                                     # last_seen_version (отслеживание смены
                                     # версии между запусками работает всегда).
    "no_browser_on_start": False,   # не открывать вкладку браузера при старте
                                     # (трейный запуск exe, см. tray_app.py).
                                     # Действует по «ИЛИ» с аргументом командной
                                     # строки --autostart: вкладка не открывается,
                                     # если сработал хотя бы один из двух способов.
    # Раздел 1 — предпочтения скачивания
    "video_ext": "original",        # original|mp4|mov|webm|mkv
    "audio_ext": "mp3",             # mp3|wav|aac|opus
    "compress_audio_kbps": 192,     # 96|128|192|256|320
    "compress_target_mb": "",       # "" = не применять сжатие до размера
    "default_clip_length_for_timestamp_link": "",  # "" = маркер конца на всей
                                     # длительности; иначе — секунды, только
                                     # когда таймкод распознан из ссылки в
                                     # «нарезать» (см. templates/trim.html)
    "rate_limit_value": "",         # "" = без ограничения скорости
    "rate_limit_unit": "MB",        # KB|MB|Kbit|Mbit
    "theme": "dark",                # dark|light
    "embed_metadata": True,         # встраивать обложку+метаданные (скачать/нарезать)
    "max_concurrent_downloads": "3",  # 1|2|3|5|unlimited — параллелизм скачивания плейлиста
    # Раздел 2 — пути
    "temp_mode": "app",             # app|system|custom — папка временных файлов
    "temp_custom_path": "",         # значим только при temp_mode="custom"
    "cache_mode": "app",            # app|system|custom — папка кэша
    "cache_custom_path": "",        # значим только при cache_mode="custom"
    "use_cookies": False,           # главный тумблер — по умолчанию выкл (см. «Cookies»)
    "cookies_from_browser": True,   # вложенный тумблер, значим только при use_cookies=True
    "cookies_browser": "firefox",   # firefox|librewolf|waterfox|zen — ТОЛЬКО Gecko, см. README
    "cookies_file_path": "",        # состояние 3: ручной cookies.txt
    "last_seen_version": "",        # версия прошлого запуска (x.ddmmyy), см.
                                     # version_change_status()/mark_version_seen();
                                     # "" — ещё ни разу не запускалось
    "seen": False,                  # флаг разового события, см. save_settings()
}

_settings = dict(DEFAULT_SETTINGS)

# Текущие рабочие пути/параметры — переустанавливаются apply_settings().
DOWNLOADS_DIR = default_temp_dir()
CACHE_DIR = default_cache_dir()
# Доступность НАСТРОЕННОГО (не подставленного) пути — см. apply_settings().
# TEMP_AVAILABLE=False блокирует операции (temp_blocked_error), CACHE_AVAILABLE
# лишь отключает кэш («мягко», без блокировки — см. этап 4).
TEMP_AVAILABLE = True
CACHE_AVAILABLE = True
# Путь, который реально настроен (даже если недоступен) — для текста ошибки/
# уведомления; отличается от DOWNLOADS_DIR/CACHE_DIR, если пришлось откатиться.
TEMP_CONFIGURED_PATH = DOWNLOADS_DIR
CACHE_CONFIGURED_PATH = CACHE_DIR
CACHE_TTL = 6 * 3600              # сколько хранить кэшированный ролик, с
YT_DLP_BIN = "yt-dlp"              # резолвится refresh_dependencies() ниже (bin/ или PATH)
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"
DENO_BIN = "deno"
MAX_FILE_BYTES = 0                # лимит отдачи файла (0 = без лимита)
YT_COOKIES_BROWSER = ""
YT_COOKIES_FILE = ""
_RATE_LIMIT = ""                  # значение для --limit-rate (напр. "2M")


_VALID_THEMES = ("dark", "light")


def _normalize_theme(value):
    """Допустимы только 'dark'/'light' — всё прочее (включая старое 'custom',
    если у кого-то уже сохранено в settings.json) тихо сводится к 'dark', без
    ошибки и без падения приложения."""
    return value if value in _VALID_THEMES else "dark"


# Только Gecko (Firefox и форки) — авто-подтяжка cookies из Chromium-браузеров
# (Chrome/Edge/Opera/Brave/Yandex) с 2024 не работает (app-bound encryption,
# ключ расшифровки привязан к самому браузеру); Firefox и форки хранят cookies
# в открытом SQLite — читаются извне. См. README «Cookies и доступ к YouTube».
_VALID_COOKIE_BROWSERS = ("firefox", "librewolf", "waterfox", "zen")


def _normalize_cookies_browser(value):
    """Допустимы только firefox/librewolf/waterfox/zen — всё прочее (включая
    старые chrome/edge/opera/vivaldi/brave/yandex до этапа 3) тихо сводится к
    'firefox', без ошибки и без падения приложения."""
    return value if value in _VALID_COOKIE_BROWSERS else "firefox"


# Допустимые значения битрейта аудио — объединение опций обоих UI:
# settings.html (128/192/256/320) и compress/trim (96/128/192/256).
_VALID_AUDIO_KBPS = (96, 128, 192, 256, 320)


def _normalize_audio_kbps(value):
    """DEFAULT_SETTINGS хранит int (192), но фронт (`<select>.value`) всегда
    шлёт строку — без коэрции сохранённое значение расходится по типу с тем,
    что сравнивают шаблоны (`settings.compress_audio_kbps == 192`), и
    предпочтение перестаёт применяться сразу после первого же изменения через
    настройки. Недопустимое/нечисловое значение тихо сводится к 192."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 192
    return v if v in _VALID_AUDIO_KBPS else 192


# Лимит одновременных загрузок плейлиста (см. youtube.py: _download_playlist_thread).
_VALID_MAX_CONCURRENT = ("1", "2", "3", "5", "unlimited")


def _normalize_max_concurrent(value):
    """Допустимы 1/2/3/5/unlimited. Всё прочее, включая '0' (который в
    youtube.py трактовался как «без ограничения»), тихо сводится к '3' —
    безопасному дефолту. 'unlimited' сохраняется как есть: это осознанный
    режим (см. REL-7)."""
    v = str(value if value is not None else "").strip().lower()
    return v if v in _VALID_MAX_CONCURRENT else "3"


def _normalize_clip_length(value):
    """"" = не задано (маркер конца на всей длительности, см. trim.html). Иначе
    должно быть числом > 0 секунд; `min="1"` на фронте — только подсказка, не
    защита, поэтому 0/отрицательное/нечисловое тихо сводится к "" — тот же
    паттерн, что у _normalize_audio_kbps и других настроек выше."""
    v = str(value if value is not None else "").strip()
    if not v:
        return ""
    try:
        n = float(v)
    except ValueError:
        return ""
    return v if n > 0 else ""


def parse_int(value, default=0):
    """Целое из пользовательского ввода без падения: None/""/нечисло ->
    default. Заменяет голый int() в API-роутах, который на мусорный JSON
    отвечал 500 (см. REL-5)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_VALID_PATH_MODES = ("app", "system", "custom")


def _normalize_path_mode(value):
    """Допустимы только app/system/custom — прочее (в т.ч. отсутствие значения
    у совсем старых настроек) тихо сводится к 'app' (см. миграция выше)."""
    return value if value in _VALID_PATH_MODES else "app"


def get_settings():
    return dict(_settings)


def load_settings():
    global _settings
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in DEFAULT_SETTINGS:
                if k in data:
                    s[k] = data[k]
            # Миграция настроек до этапа 3: главного тумблера use_cookies не
            # было — cookies подключались «по требованию» всегда, если был
            # настроен файл или включена автоподтяжка. Не терять молча уже
            # работавшую у пользователя настройку.
            if "use_cookies" not in data:
                s["use_cookies"] = bool(data.get("cookies_from_browser")) or \
                    bool((data.get("cookies_file") or "").strip())
            if "cookies_file_path" not in data and data.get("cookies_file"):
                s["cookies_file_path"] = data["cookies_file"]
            # Миграция настроек до этапа 4: одна строка temp_path/cache_path
            # ("" = системный дефолт) → раздельные режим+путь. Старое "" — НЕ
            # в режим "system" (как было по факту раньше), а в новый
            # рекомендованный дефолт "app", как для нового пользователя; уже
            # заданный пользователем свой путь — сохраняем как режим "custom",
            # чтобы не потерять его молча.
            if "temp_mode" not in data:
                old_temp = (data.get("temp_path") or "").strip()
                if old_temp:
                    s["temp_mode"] = "custom"
                    s["temp_custom_path"] = old_temp
                else:
                    s["temp_mode"] = "app"
            if "cache_mode" not in data:
                old_cache = (data.get("cache_path") or "").strip()
                if old_cache:
                    s["cache_mode"] = "custom"
                    s["cache_custom_path"] = old_cache
                else:
                    s["cache_mode"] = "app"
    except Exception:
        pass
    s["theme"] = _normalize_theme(s.get("theme"))
    s["cookies_browser"] = _normalize_cookies_browser(s.get("cookies_browser"))
    s["compress_audio_kbps"] = _normalize_audio_kbps(s.get("compress_audio_kbps"))
    s["temp_mode"] = _normalize_path_mode(s.get("temp_mode"))
    s["cache_mode"] = _normalize_path_mode(s.get("cache_mode"))
    s["default_clip_length_for_timestamp_link"] = _normalize_clip_length(
        s.get("default_clip_length_for_timestamp_link"))
    s["max_concurrent_downloads"] = _normalize_max_concurrent(
        s.get("max_concurrent_downloads"))
    _settings = s
    return s


def save_settings(partial, strict=False):
    """Обновить настройки (частично), сохранить на диск и применить.

    strict=True — пробросить наружу ошибку записи settings.json (для API,
    которое вернёт её пользователю); по умолчанию ошибка только логируется,
    чтобы внутренние вызовы (mark_version_seen при старте и т.п.) не падали
    из-за недоступного файла (см. REL-3)."""
    global _settings
    s = dict(_settings)
    for k, v in (partial or {}).items():
        if k in DEFAULT_SETTINGS:
            s[k] = v
    s["theme"] = _normalize_theme(s.get("theme"))
    s["cookies_browser"] = _normalize_cookies_browser(s.get("cookies_browser"))
    s["compress_audio_kbps"] = _normalize_audio_kbps(s.get("compress_audio_kbps"))
    s["temp_mode"] = _normalize_path_mode(s.get("temp_mode"))
    s["cache_mode"] = _normalize_path_mode(s.get("cache_mode"))
    s["default_clip_length_for_timestamp_link"] = _normalize_clip_length(
        s.get("default_clip_length_for_timestamp_link"))
    s["max_concurrent_downloads"] = _normalize_max_concurrent(
        s.get("max_concurrent_downloads"))
    _settings = s
    write_error = None
    try:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception as e:
        write_error = e
        logger.warning("Настройки: не удалось записать %s: %s", SETTINGS_FILE, e)
    apply_settings()
    logger.info("Настройки обновлены: %s", partial)
    if write_error is not None and strict:
        raise write_error
    return s


# --- Версия приложения: смена между запусками --------------------------------
# Changelog пока не реализован (сейчас нужны только сами механизмы) — здесь
# только вычисляем факт смены версии и пишем его в лог, ничего не показываем.
_VERSION_CHANGE = {"changed": False, "from": "", "to": ""}


def _compute_version_change():
    """Сравнивает last_seen_version (settings.json) с текущей
    tools.settings.VERSION_NUMBER через compare_versions() и записывает
    результат в _VERSION_CHANGE + в лог. Ничего не сохраняет — только
    вычисляет; см. mark_version_seen() для записи текущей версии как увиденной.

    Локальный импорт tools.settings — на уровне модуля core.py импортировать
    нельзя (tools.settings сам импортирует core, будет цикл)."""
    global _VERSION_CHANGE
    from tools.settings import VERSION_NUMBER
    previous = (_settings.get("last_seen_version") or "").strip()
    current = VERSION_NUMBER

    if not previous:
        # Первый запуск — сравнивать не с чем, сменой версии не считается.
        _VERSION_CHANGE = {"changed": False, "from": "", "to": current}
        logger.info("Версия приложения: первый запуск (%s).", current)
        return

    cmp = compare_versions(previous, current)
    if cmp is None:
        # Не смогли распарсить одну из версий — ведём себя как при отсутствии
        # смены (см. требование к parse_version/compare_versions), но факт
        # фиксируем в логе.
        _VERSION_CHANGE = {"changed": False, "from": previous, "to": current}
        logger.info(
            "Версия приложения: не удалось сравнить %r и %r — считаем, что "
            "версия не менялась.", previous, current)
        return

    changed = cmp != 0   # включая откат на более старую версию (cmp > 0)
    _VERSION_CHANGE = {"changed": changed, "from": previous, "to": current}
    if changed:
        logger.info("Версия приложения сменилась: %s -> %s", previous, current)
    else:
        logger.info("Версия приложения не менялась (%s).", current)


def version_change_status():
    """{"changed": bool, "from": str, "to": str} — результат последнего вызова
    _compute_version_change(). changed=True — last_seen_version отличается от
    текущей VERSION_NUMBER в любую сторону (в т.ч. откат на старую версию)."""
    return dict(_VERSION_CHANGE)


def mark_version_seen():
    """Записывает текущую VERSION_NUMBER в last_seen_version (settings.json).
    Одно поле, без отдельного флага "changelog просмотрен": совпадение
    last_seen_version с текущей версией само по себе означает, что для неё
    уже всё показано — два поля рассинхронились бы.

    Сейчас вызывается сразу при старте (см. check_version_change_at_startup).
    Когда появится окно changelog — вызов нужно будет убрать из старта и
    перенести на момент, когда пользователь закроет это окно; искать вызов
    можно будет по этому докстрингу."""
    from tools.settings import VERSION_NUMBER
    save_settings({"last_seen_version": VERSION_NUMBER})


def check_version_change_at_startup():
    """Точка входа, вызываемая один раз при старте приложения (app.py):
    вычисляет смену версии (_compute_version_change) и сразу же отмечает
    текущую версию увиденной (mark_version_seen). Обе половины идут подряд,
    ПОКА нет окна changelog — см. докстринг mark_version_seen() про то, что
    изменится, когда оно появится."""
    _compute_version_change()
    mark_version_seen()


# --- Версия приложения: проверка релизов на GitHub ---------------------------
# Тот же кэш-паттерн (ts/result + TTL), что у check_ytdlp_update() ниже, но
# свой отдельный кэш — это разные проверки разных репозиториев/эндпоинтов.
_APP_UPDATE_CACHE_TTL = 1800   # с — как у yt-dlp: не дёргать GitHub API чаще
_app_update_cache = {"ts": 0.0, "result": None}
_APP_RELEASES_LATEST_URL = "https://api.github.com/repos/Gwendarr/jade.tools/releases/latest"


def check_app_update(force=False):
    """Сравнить текущую версию приложения (tools.settings.VERSION_NUMBER) с
    последним ОПУБЛИКОВАННЫМ релизом на GitHub — releases/latest сам
    игнорирует черновики и предрелизы. Результат кэшируется на
    _APP_UPDATE_CACHE_TTL секунд (если force=False).

    Настройка check_app_updates=False отключает проверку целиком — функция
    возвращает status="disabled" СРАЗУ, до чтения кэша и до похода в сеть, и
    не трогает кэш: при повторном включении разрешён тот же кэш ~30 минут,
    что и обычно (если TTL с последней реальной проверки ещё не истёк —
    вернётся он, а не свежий запрос). НЕ влияет на last_seen_version.

    Возвращает {status: "available"|"up_to_date"|"error"|"disabled", current,
    latest}. status="available" — единственное, что должен показывать фронт
    (см. templates/_base.html); всё остальное фронт молча игнорирует.

    Любая проблема тихо трактуется как «обновлений нет», без исключения
    наружу и без сообщения пользователю — только лог:
      * нет сети / таймаут;
      * 404 — либо репозиторий приватный, либо релизов ещё не публиковали
        (сейчас верно и то и другое, это ожидаемое состояние, не ошибка);
      * лимит запросов GitHub (403/429);
      * тег не распознан parse_version() (compare_versions() вернул None)."""
    if not get_settings().get("check_app_updates", True):
        return {"status": "disabled", "current": "", "latest": ""}

    now = time.time()
    cached = _app_update_cache["result"]
    if not force and cached and now - _app_update_cache["ts"] < _APP_UPDATE_CACHE_TTL:
        return cached

    from tools.settings import VERSION_NUMBER
    current = VERSION_NUMBER

    try:
        req = urllib.request.Request(
            _APP_RELEASES_LATEST_URL, headers={"User-Agent": "jade.tools"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("tag_name") or "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info("Проверка версии приложения: релизов на GitHub пока "
                       "нет (или репозиторий приватный) — 404.")
        else:
            logger.warning("Проверка версии приложения не удалась: HTTP %s", e.code)
        result = {"status": "error", "current": current, "latest": ""}
        _app_update_cache.update(ts=now, result=result)
        return result
    except Exception as e:
        logger.warning("Проверка версии приложения не удалась: %s", e)
        result = {"status": "error", "current": current, "latest": ""}
        _app_update_cache.update(ts=now, result=result)
        return result

    cmp = compare_versions(latest, current)
    if cmp is None:
        logger.info("Проверка версии приложения: тег %r не распознан.", latest)
        result = {"status": "error", "current": current, "latest": latest}
    elif cmp > 0:
        result = {"status": "available", "current": current, "latest": latest}
        logger.info("jade.tools: доступна новая версия %s (у вас %s)", latest, current)
    else:
        result = {"status": "up_to_date", "current": current, "latest": latest}
    _app_update_cache.update(ts=now, result=result)
    return result


def start_app_update_check():
    """Запускает check_app_update() в фоновом потоке — не блокирует старт
    сервера и открытие браузера (тот же принцип, что у start_janitor()).
    Результат кэшируется и отдаётся фронту по требованию
    (см. app.py: GET /api/app_update_check).

    check_app_updates=False — поток вообще не стартует (не просто игнорирует
    результат): при выключенной настройке приложение ни разу не обращается
    к GitHub. Повторный вызов не нужен — после включения обратно проверка
    возобновляется сама, как только что-то дёрнет check_app_update() (см. её
    докстринг про тот же кэш ~30 минут)."""
    if not get_settings().get("check_app_updates", True):
        logger.info("Проверка обновлений приложения отключена в настройках — "
                   "фоновая проверка при старте не выполняется.")
        return
    def _run():
        try:
            check_app_update()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def reset_settings():
    """Вернуть все настройки к значениям по умолчанию."""
    global _settings
    _settings = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.is_file():
            SETTINGS_FILE.unlink()
    except Exception:
        pass
    apply_settings()
    logger.info("Настройки сброшены к значениям по умолчанию")
    return dict(_settings)


def _rate_limit_arg():
    """Настройка ограничения скорости → значение yt-dlp --limit-rate.
    yt-dlp принимает байты/К/М, поэтому биты пересчитываем в байты (÷8)."""
    try:
        val = float(_settings.get("rate_limit_value"))
    except (TypeError, ValueError):
        return ""
    if val <= 0:
        return ""
    unit = _settings.get("rate_limit_unit", "MB")
    if unit == "KB":
        return f"{val:g}K"
    if unit == "MB":
        return f"{val:g}M"
    if unit == "Kbit":
        return f"{val / 8:g}K"
    if unit == "Mbit":
        return f"{val / 8:g}M"
    return f"{val:g}K"


# Firefox-форки без собственного ключевого слова в yt-dlp (проверено:
# yt_dlp.cookies.SUPPORTED_BROWSERS = chromium-based | {firefox, safari} — ни
# librewolf, ни waterfox, ни zen туда не входят). Хранилище cookies у них то же
# Mozilla-совместимое SQLite, что у Firefox, поэтому резолвим путь к профилю
# вручную и передаём как "firefox:<путь>" — yt_dlp._extract_firefox_cookies
# принимает произвольный путь как корень поиска (ищет cookies.sqlite в нём
# самом, его подпапках и Profiles/*/, что соответствует реальной раскладке).
_FIREFOX_FORK_APPDATA = {"librewolf": "LibreWolf", "waterfox": "Waterfox", "zen": "zen"}

# Отображаемые названия браузеров (для текста ошибок) — та же капитализация,
# что в дропдауне настроек (templates/settings.html).
_COOKIE_BROWSER_LABELS = {
    "firefox": "Firefox", "librewolf": "LibreWolf",
    "waterfox": "Waterfox", "zen": "Zen",
}


def _resolve_cookies_browser_spec(browser_key):
    """Значение для --cookies-from-browser по выбору в настройках."""
    appdata_name = _FIREFOX_FORK_APPDATA.get(browser_key)
    if not appdata_name:
        spec = "firefox"
        logger.info("Cookies: резолвлен --cookies-from-browser = %s", spec)
        return spec
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    spec = f"firefox:{Path(appdata) / appdata_name}"
    logger.info("Cookies: резолвлен --cookies-from-browser = %s", spec)
    return spec


def _resolve_mode_path(mode, custom_path, app_fn, system_fn):
    """Путь-кандидат (ещё НЕ проверенный на доступность) для режима app|
    system|custom. custom с пустым путём трактуется как app (нечего
    подставлять в поле — не блокировать же операции из-за незаполненного
    "свой путь", пока пользователь его не указал)."""
    if mode == "system":
        return system_fn()
    if mode == "custom":
        cp = (custom_path or "").strip()
        if cp:
            return Path(cp)
    return app_fn()


def apply_settings():
    """Применить настройки к рабочим путям/параметрам (после load/save/reset).

    temp/cache резолвятся по режиму (app|system|custom, см. _resolve_mode_path)
    и проверяются на доступность (_dir_available) ДО попытки создания — по
    этой проверке выставляются TEMP_AVAILABLE/CACHE_AVAILABLE и запоминается
    TEMP_CONFIGURED_PATH/CACHE_CONFIGURED_PATH (реально настроенный путь, даже
    если он недоступен — нужен для текста ошибки/уведомления). Если
    настроенный путь недоступен, рабочий каталог (DOWNLOADS_DIR/CACHE_DIR)
    всё равно откатывается на режим "app" — ТОЛЬКО чтобы не уронить
    приложение (JOBS и т.п. должны на что-то указывать), но это НЕ считается
    исправлением: TEMP_AVAILABLE остаётся False, операции скачивания
    блокируются (см. temp_blocked_error()), пока пользователь не подтвердит
    переключение на app явно (через модалку) или не почини т свой путь —
    молча подсовывать другую папку без его ведома нельзя."""
    global DOWNLOADS_DIR, CACHE_DIR, TEMP_AVAILABLE, CACHE_AVAILABLE
    global TEMP_CONFIGURED_PATH, CACHE_CONFIGURED_PATH
    global YT_COOKIES_BROWSER, YT_COOKIES_FILE, _RATE_LIMIT

    TEMP_CONFIGURED_PATH = _resolve_mode_path(
        _settings.get("temp_mode"), _settings.get("temp_custom_path"),
        default_temp_dir, system_temp_dir)
    TEMP_AVAILABLE = _dir_available(TEMP_CONFIGURED_PATH)
    if TEMP_AVAILABLE:
        DOWNLOADS_DIR = TEMP_CONFIGURED_PATH
        try:
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            TEMP_AVAILABLE = False
    if not TEMP_AVAILABLE:
        # Откат — раньше уходил на старый системный дефолт, теперь на app
        # (см. этап 4). Только чтобы JOBS было куда писать; проблему это не
        # решает — см. docstring выше.
        try:
            DOWNLOADS_DIR = default_temp_dir()
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            DOWNLOADS_DIR = Path(tempfile.gettempdir()) / "jade.tools"
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    CACHE_CONFIGURED_PATH = _resolve_mode_path(
        _settings.get("cache_mode"), _settings.get("cache_custom_path"),
        default_cache_dir, system_cache_dir)
    CACHE_AVAILABLE = _dir_available(CACHE_CONFIGURED_PATH)
    if CACHE_AVAILABLE:
        CACHE_DIR = CACHE_CONFIGURED_PATH
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            CACHE_AVAILABLE = False
    if not CACHE_AVAILABLE:
        # Кэш — опциональный (только ускорение): мягкий откат на app, кэш
        # просто выключен (cache_get/cache_put), без блокировки чего-либо.
        try:
            CACHE_DIR = default_cache_dir()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            CACHE_DIR = Path(tempfile.gettempdir()) / "jade.tools" / "cache"
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Главный тумблер «использовать cookies» — выключен по умолчанию. Выключен
    # → cookies не подключаются ни при каких обстоятельствах (см. cookies_args()).
    if _settings.get("use_cookies") and _settings.get("cookies_from_browser"):
        YT_COOKIES_BROWSER = _resolve_cookies_browser_spec(
            _normalize_cookies_browser(_settings.get("cookies_browser")))
        YT_COOKIES_FILE = ""
    elif _settings.get("use_cookies"):
        YT_COOKIES_BROWSER = ""
        YT_COOKIES_FILE = (_settings.get("cookies_file_path") or "").strip()
    else:
        YT_COOKIES_BROWSER = ""
        YT_COOKIES_FILE = ""
    _RATE_LIMIT = _rate_limit_arg()


def temp_blocked_error():
    """Текст ошибки, если temp недоступен (см. apply_settings), или "" если
    всё в порядке. Проверяется в API-роутах, которые пишут в DOWNLOADS_DIR
    (upload/download/start/prepare во всех трёх инструментах), ДО начала
    операции — блокирует скачивание/обработку, пока пользователь не
    подтвердит переключение на «в папке приложения» (модалка на главной) или
    не почини т свой путь вручную (см. этап 4). cache — НЕ блокирует ничего,
    только мягко отключается (см. cache_get/cache_put)."""
    if TEMP_AVAILABLE:
        return ""
    return (f"Папка временных файлов недоступна: {TEMP_CONFIGURED_PATH}. "
            f"Откройте настройки (раздел «Пути») и переключите на «в папке "
            f"приложения», либо укажите доступный путь.")


def paths_status():
    """Статус temp/cache для баннера/модалки на главной и пульсации строк в
    настройках: {temp: {available, path, mode}, cache: {...}}. path — реально
    НАСТРОЕННЫЙ путь (см. *_CONFIGURED_PATH), не откат."""
    return {
        "temp": {"available": TEMP_AVAILABLE, "path": str(TEMP_CONFIGURED_PATH),
                 "mode": _settings.get("temp_mode") or "app"},
        "cache": {"available": CACHE_AVAILABLE, "path": str(CACHE_CONFIGURED_PATH),
                  "mode": _settings.get("cache_mode") or "app"},
    }


load_settings()
apply_settings()


def _cache_subdir(key):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / h


def cache_get(key):
    """Путь к ранее скачанному файлу для данного ключа спецификации, или None.
    Если настроенная папка кэша недоступна (CACHE_AVAILABLE=False) — кэш
    просто выключен (см. этап 4): CACHE_DIR в этом случае указывает на
    app-фоллбэк только чтобы не уронить приложение, но реально в него ничего
    не пишем и не читаем — иначе кэш незаметно переехал бы без ведома
    пользователя вместо честного «временно отключён»."""
    if not CACHE_AVAILABLE:
        return None
    d = _cache_subdir(key)
    ready = d / ".ready"
    if not ready.is_file():
        return None
    files = [p for p in d.iterdir() if p.is_file() and p.name != ".ready"]
    if not files:
        return None
    try:
        ready.touch()             # продлеваем срок жизни при обращении
    except Exception:
        pass
    result = str(max(files, key=lambda p: p.stat().st_size))
    logger.info("Кэш: найден готовый файл (ключ=%s) -> %s", key, Path(result).name)
    return result


def cache_put(key, src_path):
    """Положить скачанный файл в кэш (копией). Возвращает путь в кэше или
    исходный путь, если положить не удалось (в т.ч. если кэш временно
    отключён — см. cache_get)."""
    if not CACHE_AVAILABLE:
        return src_path
    try:
        d = _cache_subdir(key)
        d.mkdir(parents=True, exist_ok=True)
        dst = d / Path(src_path).name
        if Path(src_path).resolve() != dst.resolve():
            shutil.copy2(src_path, dst)
        (d / ".ready").write_text("1", encoding="utf-8")
        logger.info("Кэш: сохранён файл (ключ=%s) -> %s", key, dst.name)
        return str(dst)
    except Exception as e:
        logger.warning("Кэш: не удалось сохранить файл (ключ=%s): %s", key, e)
        return src_path

# --- Cookies для YouTube -----------------------------------------------------
# Главный тумблер «использовать cookies» (use_cookies, выкл по умолчанию) —
# если выключен, cookies не подключаются вообще, ни при каких обстоятельствах.
# Включён → один из двух вложенных режимов (см. apply_settings):
#   * cookies_from_browser → --cookies-from-browser <browser> (читаются заново
#     при каждом скачивании, не устаревают; ТОЛЬКО Firefox/форки — Chromium с
#     2024 шифрует cookies так, что внешний процесс их прочитать не может);
#   * иначе — путь к файлу .txt (формат Netscape, любой браузер — экспорт
#     делает сам пользователь расширением внутри браузера).
def cookies_args():
    """Аргументы yt-dlp для передачи cookies (или пустой список, если
    «использовать cookies» выключено — см. use_cookies)."""
    if not _settings.get("use_cookies"):
        return []
    if YT_COOKIES_BROWSER:
        logger.info("yt-dlp: команда получит --cookies-from-browser %s", YT_COOKIES_BROWSER)
        return ["--cookies-from-browser", YT_COOKIES_BROWSER]
    if YT_COOKIES_FILE and os.path.isfile(YT_COOKIES_FILE):
        return ["--cookies", YT_COOKIES_FILE]
    fb = BASE_DIR / "cookies.txt"      # запасной файл рядом с приложением (ручной режим)
    if fb.is_file():
        return ["--cookies", str(fb)]
    return []


def resolve_cookies_file():
    """Путь к cookies.txt для отображения в настройках (раздел «файлы» — кнопка
    «показать»): явно заданный в настройках путь, если указывает на
    существующий файл, иначе дефолтный `cookies.txt` рядом с приложением. "" —
    если ничего не найдено. В отличие от YT_COOKIES_FILE (которая пуста, пока
    use_cookies выключен или включён режим «из браузера») — не зависит от
    состояния тумблеров, показывает файл независимо от того, используется ли
    он прямо сейчас."""
    manual = (_settings.get("cookies_file_path") or "").strip()
    if manual and os.path.isfile(manual):
        return manual
    fb = BASE_DIR / "cookies.txt"
    if fb.is_file():
        return str(fb)
    return ""


def ensure_js_runtime_on_path():
    """Сделать JS-рантайм (Deno) видимым в PATH процесса.

    yt-dlp нужен JS-рантайм, чтобы решать JS-проверки YouTube (signature/n
    challenge). winget ставит deno, но не всегда добавляет его в PATH уже
    запущенного процесса (особенно exe из автозапуска) — ищем deno в типичных
    местах и дописываем в PATH. Безопасно: ничего не меняем, если deno нет."""
    if shutil.which("deno"):
        return
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
    dirs = [home / ".deno" / "bin"]
    pkgs = local / "Microsoft" / "WinGet" / "Packages"
    if pkgs.is_dir():
        dirs += list(pkgs.glob("DenoLand.Deno*"))
    for d in dirs:
        if (d / "deno.exe").is_file():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return


# --- Резолвинг внешних зависимостей ------------------------------------------
# Порядок для КАЖДОЙ зависимости: bin/ (рядом с exe, плоско) -> системный PATH
# (для Deno — через ensure_js_runtime_on_path() выше) -> нигде не найдено
# ("отсутствует", без падения приложения — окно установки это отдельный этап).
# ffmpeg и ffprobe — два разных бинарника, резолвятся и проверяются отдельно:
# один есть, другого нет — зависимость неполная.
_DEP_EXE_NAME = {
    "yt-dlp": "yt-dlp.exe",
    "ffmpeg": "ffmpeg.exe",
    "ffprobe": "ffprobe.exe",
    "deno": "deno.exe",
}

_dep_status = {}   # name -> {"path": str|None, "source": "bin"|"system"|None}


def resolve_dependency(name):
    """(путь, источник) для одной зависимости: bin/ -> PATH -> (None, None)."""
    bin_path = BIN_DIR / _DEP_EXE_NAME.get(name, name + ".exe")
    if bin_path.is_file():
        return str(bin_path), "bin"
    if name == "deno":
        ensure_js_runtime_on_path()   # winget не всегда виден в PATH запущенного exe
    found = shutil.which(name)
    return (found, "system") if found else (None, None)


def refresh_dependencies():
    """Пересчитать резолвинг всех известных зависимостей — вызывать при
    старте и после любого изменения в bin/ (например, будущая установка).
    Обновляет и статус (dependency_status), и «голые» пути для subprocess
    (YT_DLP_BIN/FFMPEG_BIN/FFPROBE_BIN/DENO_BIN)."""
    global YT_DLP_BIN, FFMPEG_BIN, FFPROBE_BIN, DENO_BIN, _dep_status
    _dep_status = {}
    for name in _DEP_EXE_NAME:
        path, source = resolve_dependency(name)
        _dep_status[name] = {"path": path, "source": source}
    YT_DLP_BIN = _dep_status["yt-dlp"]["path"] or "yt-dlp"
    FFMPEG_BIN = _dep_status["ffmpeg"]["path"] or "ffmpeg"
    FFPROBE_BIN = _dep_status["ffprobe"]["path"] or "ffprobe"
    DENO_BIN = _dep_status["deno"]["path"] or "deno"


def dependency_status():
    """Статус всех известных зависимостей: {name: {path, source, found}} —
    для будущей страницы установки и статусов в настройках."""
    if not _dep_status:
        refresh_dependencies()
    return {name: {**info, "found": info["path"] is not None}
            for name, info in _dep_status.items()}


# Какие зависимости нужны конкретному инструменту — для per-feature проверки
# (см. missing_dependencies) перед началом операции. НЕ используется для
# блокировки старта приложения в целом.
TOOL_DEPENDENCIES = {
    "youtube": ("yt-dlp", "ffmpeg", "ffprobe", "deno"),
    "compress": ("ffmpeg", "ffprobe"),
    "trim": ("ffmpeg", "ffprobe"),
}

_DEP_LABELS = {"yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "deno": "Deno"}


def missing_dependencies(tool_id):
    """Список недостающих зависимостей инструмента (пусто = всё на месте)."""
    status = dependency_status()
    return [name for name in TOOL_DEPENDENCIES.get(tool_id, ())
            if not status[name]["found"]]


def missing_dependencies_payload(tool_id):
    """Структурированный ответ для API-роутов, когда инструменту не хватает
    зависимостей — фронт по нему открывает окно установки (этап 2), а не
    просто показывает текст ошибки. None, если всё на месте. Проверяется ДО
    начала операции, а не реактивно после падения subprocess."""
    missing = missing_dependencies(tool_id)
    if not missing:
        return None
    names = ", ".join(_DEP_LABELS.get(n, n) for n in missing)
    return {
        "error": (f"Не найдены необходимые компоненты: {names}. Установите их "
                  f"и повторите."),
        "missing_deps": missing,
        "need_install": True,
    }


# Группировка для окна установки/настроек: ffmpeg и ffprobe — один релиз/архив
# BtbN, поэтому один пункт «ffmpeg + ffprobe» с общей ссылкой/статусом; у
# каждой группы — pip- ИЛИ winget-команда (никогда обе), фоллбэк — всегда
# ссылка на релиз GitHub (release_page_url, определена ниже вместе с
# остальной логикой установки — вызывается только по запросу, не при импорте).
DEP_GROUPS = {
    "yt-dlp": {"label": "yt-dlp", "deps": ("yt-dlp",),
              "pip_cmd": "pip install -U yt-dlp", "winget_cmd": None},
    "ffmpeg": {"label": "ffmpeg + ffprobe", "deps": ("ffmpeg", "ffprobe"),
              "pip_cmd": None, "winget_cmd": "winget install -e --id Gyan.FFmpeg"},
    "deno": {"label": "Deno", "deps": ("deno",),
            "pip_cmd": None, "winget_cmd": "winget install -e --id DenoLand.Deno"},
}


def dependency_groups():
    """Зависимости, сгруппированные для окна установки (варианты 2/3) и
    раздела настроек «расположение зависимостей»: {key: {label, deps, found,
    source, release_url, pip_cmd, winget_cmd}}. source — общий источник всех
    зависимостей группы ("bin"/"system"), "mixed", если они разошлись
    (на практике не должно случаться — ffmpeg/ffprobe ставятся вместе), или
    None, если группа не найдена вовсе."""
    status = dependency_status()
    out = {}
    for key, meta in DEP_GROUPS.items():
        members = [status[d] for d in meta["deps"]]
        found = all(m["found"] for m in members)
        sources = {m["source"] for m in members if m["source"]}
        source = sources.pop() if len(sources) == 1 else ("mixed" if sources else None)
        out[key] = {
            "label": meta["label"], "deps": list(meta["deps"]),
            "found": found, "source": source,
            "release_url": release_page_url(key),
            "pip_cmd": meta["pip_cmd"], "winget_cmd": meta["winget_cmd"],
        }
    return out


# Резолвим зависимости сразу при импорте (bin/ -> PATH), включая подхват Deno
# в PATH процесса (до первых вызовов yt-dlp).
refresh_dependencies()

# --- Авто-очистка temp/ -------------------------------------------------------
# Рабочие файлы задач — временные: браузер всё равно скачивает результат себе,
# поэтому копию в temp/ держать незачем. Уборщик (janitor) ниже удаляет их.
CLEANUP_AFTER_SERVE = 30     # с после отдачи файла в браузер (даём запас на retry)
CLEANUP_IDLE_DONE   = 600    # с для завершённых, но так и не скачанных задач
CLEANUP_IDLE_STALE  = 1800   # с для брошенных задач (напр. загруженный, но не сжатый файл)
_JANITOR_INTERVAL   = 15     # как часто уборщик проверяет задачи, с

# На Windows скрывает консольное окно дочернего процесса (yt-dlp/ffmpeg).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- Задачи ------------------------------------------------------------------

JOBS_LOCK = threading.Lock()
JOBS = {}            # job_id -> dict
_MAX_DONE_JOBS = 20  # сколько завершённых задач хранить в памяти

# Активные задачи трогать нельзя (идёт работа); терминальные — можно убирать.
_ACTIVE_STATUSES   = ("pending", "fetching", "downloading", "processing")
_TERMINAL_STATUSES = ("done", "error", "canceled")


def is_job_active(job):
    """True, если задача уже выполняется/в очереди на выполнение. Нужно API-
    роутам, чтобы повторный/параллельный запуск той же задачи (двойной клик,
    повторный fetch) получал 409, а не запускал вторую работу на ту же папку
    (см. REL-2)."""
    return bool(job) and job.get("status") in _ACTIVE_STATUSES


def new_job(initial=None):
    job = {
        "status": "pending",   # pending|fetching|ready|downloading|processing
                                # |done|error|canceled
        "progress": 0.0,        # 0..100
        "speed": "",
        "eta": "",
        "downloaded_mb": 0.0,    # для задач с известным Content-Length (установка зависимостей)
        "total_mb": 0.0,
        "stage": "",            # текстовая подсказка о текущем этапе
        "title": "",
        "filename": "",         # итоговый путь к файлу на диске
        "download_name": "",    # имя для отдачи в браузер
        "error": "",
        "info": None,           # кэш данных (для качалки — список форматов)
        "proc": None,           # активный subprocess (для отмены)
    }
    if initial:
        job.update(initial)
    return job


def job_dir(job_id):
    d = DOWNLOADS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_old_jobs():
    """Удаляет файлы и записи старых завершённых задач. Вызывать под JOBS_LOCK."""
    terminal = [jid for jid, j in JOBS.items()
                if j["status"] in ("done", "error", "canceled")]
    for jid in terminal[_MAX_DONE_JOBS:]:
        shutil.rmtree(DOWNLOADS_DIR / jid, ignore_errors=True)
        del JOBS[jid]


def cleanup_all_jobs(*_):
    """Останавливает активные дочерние процессы (yt-dlp/ffmpeg) и удаляет все
    временные папки задач — при выходе из приложения. Лок не берём: функция
    вызывается и из обработчика сигнала, где захват JOBS_LOCK мог бы дать
    взаимоблокировку, если сигнал пришёл, пока поток держит лок (см. REL-4)."""
    procs = [job.get("proc") for job in list(JOBS.values()) if job.get("proc")]
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    try:
        for job_id in list(JOBS):
            shutil.rmtree(DOWNLOADS_DIR / job_id, ignore_errors=True)
    except Exception:
        pass


def schedule_cleanup(job, delay=CLEANUP_AFTER_SERVE):
    """Назначить удаление ТОЛЬКО ЧТО ОТДАННОГО результата через delay секунд.

    Вызывается после отдачи готового файла в браузер: копия в downloads/
    больше не нужна. Задачу и исходник (job['src_path']) это не трогает —
    пока задача жива, тем же job_id можно пересчитать результат другими
    настройками (см. tools/compress.py). Вызывать под JOBS_LOCK."""
    path = job.get("filename")
    if not path:
        return
    served = [e for e in job.get("_served_files", []) if e[0] != path]
    served.append((path, time.time() + delay))
    job["_served_files"] = served


def clear_work_dir(work, keep_prefix="source"):
    """Удаляет содержимое рабочей папки задачи, кроме файлов/папок, чьё имя
    начинается с keep_prefix. Используется при отмене или повторной обработке
    в tools/compress.py, чтобы не терять уже полученный исходник."""
    try:
        entries = list(work.iterdir())
    except OSError:
        return
    for p in entries:
        if p.name.startswith(keep_prefix):
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass


# --- Уборщик (janitor): фоновое удаление старых файлов из temp/ --------------

# Имена каталогов, которые приложение создаёт САМО в temp/ (DOWNLOADS_DIR):
#   * <job_id>            — uuid4().hex[:12] (см. job_dir/новые задачи);
#   * ytdlp-update-<8hex> — временная папка самообновления yt-dlp
#                           (см. _update_ytdlp_bin).
# Всё прочее в temp/ приложению НЕ принадлежит: если пользователь указал
# «своим путём» постороннюю папку, её содержимое автоочистка не трогает
# (иначе один только старт приложения сносил бы чужие подпапки — см. REL-1).
_JOB_DIR_RE = re.compile(r'^[0-9a-f]{12}$')
_YTDLP_UPDATE_DIR_RE = re.compile(r'^ytdlp-update-[0-9a-f]{8}$')
# Кэш: <sha1(key)[:16]> — см. _cache_subdir.
_CACHE_DIR_RE = re.compile(r'^[0-9a-f]{16}$')


def is_app_job_dir(name):
    """True, если имя подпапки temp/ создано приложением (job_id или временная
    папка self-update yt-dlp) — только такие каталоги вправе удалять уборщик."""
    return bool(_JOB_DIR_RE.match(name) or _YTDLP_UPDATE_DIR_RE.match(name))


def is_cache_dir(name):
    """True, если имя подпапки кэша создано приложением (_cache_subdir)."""
    return bool(_CACHE_DIR_RE.match(name))


def _purge_orphans():
    """При старте удалить остатки прошлых запусков (подпапки temp/, созданные
    приложением) — после краша/убийства процесса живых задач на старте нет.
    Удаляются только каталоги с именами приложения (is_app_job_dir), чтобы не
    задеть пользовательские данные, если temp указывает на чужую папку."""
    removed = 0
    try:
        for entry in DOWNLOADS_DIR.iterdir():
            if entry.is_dir() and is_app_job_dir(entry.name):
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    except Exception:
        pass
    if removed:
        logger.info("Уборщик: удалено %s остатков предыдущего запуска", removed)


def _sweep_cache():
    """Удалить кэшированные ролики, к которым давно не обращались (по .ready).
    Рассматриваются только каталоги, созданные приложением (is_cache_dir) —
    если cache указывает на чужую папку, её содержимое не трогается (REL-1b)."""
    now = time.time()
    try:
        for d in CACHE_DIR.iterdir():
            if not d.is_dir() or not is_cache_dir(d.name):
                continue
            ready = d / ".ready"
            mtime = ready.stat().st_mtime if ready.is_file() else 0
            if now - mtime > CACHE_TTL:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _sweep_once():
    """Один проход уборщика: убрать отданные файлы и отлежавшиеся задачи.

    Логика по статусу:
      * активные — не трогаем вообще (ни отданные файлы, ни срок жизни), и
        сбрасываем им таймер простоя — на случай повторного запуска того же
        job_id (напр. при повторном сжатии другими настройками);
      * неактивные — уже отданные в браузер файлы результата (см.
        schedule_cleanup) удаляются по своему сроку, независимо от задачи;
        сама задача (и её исходник) живёт, пока не истечёт срок простоя,
        отсчитанный от момента, когда она впервые замечена «отлежавшейся»:
        завершённые — CLEANUP_IDLE_DONE, прочие неактивные — CLEANUP_IDLE_STALE.
    """
    now = time.time()
    due = []
    file_due = []
    with JOBS_LOCK:
        for jid, job in list(JOBS.items()):
            status = job.get("status")
            if status in _ACTIVE_STATUSES:
                job.pop("_idle_since", None)
                continue
            served = job.get("_served_files")
            if served:
                keep = [(p, at) for p, at in served if now < at]
                file_due.extend(p for p, at in served if now >= at)
                if keep:
                    job["_served_files"] = keep
                else:
                    job.pop("_served_files", None)
            if "_idle_since" not in job:
                job["_idle_since"] = now
            ttl = (CLEANUP_IDLE_DONE if status in _TERMINAL_STATUSES
                   else CLEANUP_IDLE_STALE)
            if now >= job["_idle_since"] + ttl:
                due.append(jid)

    # Удаляем файлы/папки вне локов (это IO).
    for path in file_due:
        try:
            os.remove(path)
        except OSError:
            pass

    # Запись о задаче убираем только если папка реально удалилась — иначе
    # (файл залочен) повторим в следующий проход.
    for jid in due:
        d = DOWNLOADS_DIR / jid
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            with JOBS_LOCK:
                JOBS.pop(jid, None)


def _janitor_loop():
    while True:
        time.sleep(_JANITOR_INTERVAL)
        try:
            _sweep_once()
            _sweep_cache()
        except Exception:
            pass


def start_janitor():
    """Запустить фонового уборщика (один раз). Чистит остатки прошлых запусков
    и периодически удаляет отлежавшиеся задачи."""
    _purge_orphans()
    t = threading.Thread(target=_janitor_loop, name="jade-janitor", daemon=True)
    t.start()
    logger.info("Уборщик запущен (интервал %s с)", _JANITOR_INTERVAL)
    return t


# --- yt-dlp ------------------------------------------------------------------

def _is_youtube_url(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return "youtube.com" in host or "youtu.be" in host


_ALLOWED_URL_SCHEMES = ("http://", "https://")


def is_supported_url(url):
    """True для ссылок http:// или https://. Только такие принимаются от
    пользователя: это отсекает file:// и прочие схемы (SSRF/чтение локальных
    файлов, см. SEC-11), а заодно строки, начинающиеся с "-", которые yt-dlp
    иначе принял бы за свои опции (см. SEC-4)."""
    return isinstance(url, str) and url.strip().lower().startswith(_ALLOWED_URL_SCHEMES)


def ytdlp_cmd(*extra, no_playlist=True, cookies=False):
    """cookies=True подмешивает --cookies-from-browser/--cookies (см.
    cookies_args()) — вызывающий код сам решает, нужны ли они именно сейчас
    (см. run_ytdlp_json_ex/detect_playlist/run_ytdlp_download и
    _first_attempt_cookies(): порядок первой/второй попытки зависит от
    настройки «авто cookies из браузера», но в любом случае это ровно две
    попытки в противоположные стороны, не «всегда с cookies»). Устаревшие или
    просто «чужие» для сайта cookies из того же файла/браузера сами могут
    триггерить его антибот (проверено на TikTok — с cookies стабильный HTTP
    403, без них ролик отдаётся мгновенно), поэтому навязывать их всем сайтам
    сразу же не стоит."""
    cmd = [YT_DLP_BIN]
    # Всегда безопасные базовые флаги.
    cmd += ["--no-warnings", "--no-color", "--no-progress"]
    # По умолчанию — только одно видео, даже если ссылка содержит list=...
    # Плейлист-логика (youtube.py) явно отключает это (no_playlist=False).
    if no_playlist:
        cmd += ["--no-playlist"]
    if cookies:
        cmd += cookies_args()
    # Ограничение скорости скачивания (если задано в настройках).
    if _RATE_LIMIT:
        cmd += ["--limit-rate", _RATE_LIMIT]
    # EJS solver нужен только для YouTube (решает его JS-проверку подписи —
    # без него web-клиент отдаёт только превью-картинки); на прочих сайтах он
    # не нужен, а сама ссылка всегда передаётся последним позиционным
    # аргументом во всех вызовах.
    url = extra[-1] if extra and isinstance(extra[-1], str) else ""
    if _is_youtube_url(url):
        cmd += ["--remote-components", "ejs:github"]
    # URL — последний позиционный аргумент. "--" завершает разбор опций,
    # иначе строка, начинающаяся с "-", была бы исполнена yt-dlp как его
    # собственная опция (например --exec=...), а не распознана как ссылка —
    # см. SEC-4.
    if extra:
        cmd += list(extra[:-1])
        cmd += ["--", extra[-1]]
    return cmd


def friendly_ytdlp_error(stderr):
    """Короткое понятное сообщение из stderr yt-dlp (для показа пользователю)."""
    s = (stderr or "").strip()
    if s:
        logger.warning("yt-dlp error: %s", s[:1000])
    low = s.lower()
    if "too many requests" in low or "http error 429" in low:
        return ("YouTube временно ограничил запросы с вашего адреса (429). "
                "Подождите 10–30 минут и попробуйте снова.")
    if "could not find" in low and "cookies database" in low:
        return ("Не удалось найти профиль выбранного браузера (Firefox/"
                "LibreWolf/Waterfox/Zen) — он не установлен в системе или ещё ни "
                "разу не запускался. Проверьте выбор браузера в настройках "
                "(раздел «Пути» → «использовать cookies»), либо переключитесь "
                "на файл cookies.txt.")
    if "could not copy" in low and "cookie" in low:
        return ("Не удалось прочитать cookies: браузер запущен и держит базу "
                "cookies залоченной. Закройте браузер и повторите, либо в "
                "настройках (раздел «Пути») переключитесь на файл cookies.txt.")
    if "mutagen" in low:
        return ("Для встраивания обложки/метаданных в аудио yt-dlp нужен "
                "Python-модуль mutagen: py -m pip install mutagen. Либо "
                "выключите «встраивать обложку и метаданные» в настройках.")
    if "confirm you" in low and "bot" in low:
        settings = get_settings()
        if not settings.get("use_cookies"):
            return ("YouTube требует подтвердить, что вы не бот. Включите "
                    "«использовать cookies» в настройках (раздел «Пути») — см. "
                    "README, раздел «Cookies и доступ к YouTube».")
        if settings.get("cookies_from_browser"):
            browser_key = _normalize_cookies_browser(settings.get("cookies_browser"))
            browser_label = _COOKIE_BROWSER_LABELS.get(browser_key, "браузере")
            return (f"YouTube отклонил переданные cookies и требует подтвердить, "
                    f"что вы не бот. Вероятно, сессия в {browser_label} устарела "
                    f"или была отозвана (YouTube периодически ротирует токены "
                    f"безопасности — это не связано с настройками приложения). "
                    f"Зайдите на youtube.com в {browser_label} и убедитесь, что "
                    f"вход в аккаунт всё ещё активен, при необходимости "
                    f"перелогиньтесь и повторите.")
        return ("YouTube отклонил переданные cookies и требует подтвердить, что "
                "вы не бот. Вероятно, сессия в файле cookies.txt устарела или "
                "была отозвана (YouTube периодически ротирует токены "
                "безопасности — это не связано с настройками приложения). "
                "Зайдите на youtube.com в браузере, где экспортировали "
                "cookies.txt, убедитесь, что вход в аккаунт всё ещё активен, и "
                "переэкспортируйте файл заново.")
    if ("signature solving failed" in low or "n challenge" in low
            or "only images are available" in low
            or "requested format is not available" in low):
        return ("YouTube требует решения JS-проверки — yt-dlp не смог получить "
                "форматы. Нужен JavaScript-рантайм Deno (см. README, раздел "
                "«Cookies и доступ к YouTube»); возможно, также cookies.")
    if "drm" in low:
        return "Видео защищено DRM — скачать его через yt-dlp нельзя."
    if "private video" in low or "sign in if you" in low:
        return "Видео приватное или требует входа в аккаунт (нужны cookies)."
    if "video unavailable" in low or "video is unavailable" in low:
        return "Видео недоступно (удалено, скрыто или заблокировано по региону)."
    last = [ln for ln in s.splitlines() if ln.strip()]
    return last[-1][-300:] if last else "yt-dlp вернул ошибку."


def _run_ytdlp_json_once(url, cookies):
    try:
        proc = subprocess.run(
            ytdlp_cmd("--dump-single-json", url, cookies=cookies),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp: превышено время ожидания для %s", url)
        return None, "Превышено время ожидания yt-dlp."
    except FileNotFoundError:
        logger.error("yt-dlp не найден в PATH")
        return None, "yt-dlp не найден — установите его и проверьте PATH."
    if proc.returncode != 0:
        return None, friendly_ytdlp_error(proc.stderr)
    try:
        return json.loads(proc.stdout), ""
    except Exception:
        return None, "Не удалось разобрать ответ yt-dlp."


def _cookies_enabled():
    """Главный тумблер «использовать cookies» (use_cookies) — выключен по
    умолчанию. Выключен → cookies не подключаются вообще, и тогда попытка «с
    cookies» была бы точной копией попытки «без них» (см. cookies_args()) —
    незачем дублировать вызов yt-dlp дважды впустую."""
    return bool(get_settings().get("use_cookies"))


def _first_attempt_cookies():
    """Если «использовать cookies» выключено — обе попытки бессмысленны,
    первая (и единственная) идёт без cookies. Если включено и «подтягивать
    автоматически из браузера» тоже включено — первая попытка сразу с
    cookies: YouTube (самый частый случай) их почти всегда требует, так что
    попытка вслепую без них — заведомо лишний вызов yt-dlp. Если автоподтяжка
    выключена (настроен файл) — первая попытка по-прежнему без cookies:
    чужие/устаревшие cookies сами могут триггерить антибот сайта (см.
    cookies_args() и README «Cookies и доступ к YouTube» — пример с TikTok,
    где именно так и было)."""
    if not _cookies_enabled():
        return False
    return bool(get_settings().get("cookies_from_browser"))


def run_ytdlp_json_ex(url):
    """Метаданные видео через --dump-single-json. Возвращает (dict|None, error).

    Если «использовать cookies» выключено — ровно одна попытка (вторая была
    бы идентичной, см. _cookies_enabled). Иначе порядок попыток зависит от
    настройки «авто cookies из браузера» (_first_attempt_cookies()) — если
    включена, первая попытка сразу с cookies; если выключена, первая попытка
    без них. В любом случае вторая попытка — в противоположную сторону: это
    осознанный fallback, а не «на всякий случай» — без него ломается ровно тот
    сценарий, ради которого cookies вообще подключаются «по требованию», а не
    всегда (TikTok со старыми cookies из общего файла/браузера отвечает 403,
    но без cookies отдаёт ролик мгновенно — см. README). Если провалились обе
    попытки — отдаём ошибку второй, она обычно информативнее."""
    t0 = time.time()
    first = _first_attempt_cookies()
    info, err = _run_ytdlp_json_once(url, cookies=first)
    if info is not None:
        logger.info("run_ytdlp_json_ex: %.0f мс, 1 попытка (cookies=%s) %s",
                    (time.time() - t0) * 1000, first, url)
        return info, ""
    if not _cookies_enabled():
        return None, err
    info2, err2 = _run_ytdlp_json_once(url, cookies=not first)
    logger.info("run_ytdlp_json_ex: %.0f мс, 2 попытки %s",
                (time.time() - t0) * 1000, url)
    if info2 is not None:
        return info2, ""
    return None, err2 or err


def run_ytdlp_json(url):
    """Получить метаданные видео через --dump-single-json. dict или None."""
    info, _ = run_ytdlp_json_ex(url)
    return info


def _detect_playlist_once(url, cookies):
    try:
        proc = subprocess.run(
            ytdlp_cmd("--flat-playlist", "--dump-single-json", url,
                     no_playlist=False, cookies=cookies),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp: превышено время ожидания (flat-playlist) для %s", url)
        return False, None, "Превышено время ожидания yt-dlp."
    except FileNotFoundError:
        logger.error("yt-dlp не найден в PATH")
        return False, None, "yt-dlp не найден — установите его и проверьте PATH."
    if proc.returncode != 0:
        return False, None, friendly_ytdlp_error(proc.stderr)
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return False, None, "Не удалось разобрать ответ yt-dlp."
    is_playlist = data.get("_type") == "playlist" or isinstance(data.get("entries"), list)
    return is_playlist, data, ""


def detect_playlist(url):
    """Быстро определить: одиночное видео или плейлист — БЕЗ полной загрузки
    метаданных каждого видео (--flat-playlist). Для плейлиста это за секунды
    отдаёт entries с базовыми полями (id/title/duration/thumbnails), не пытаясь
    получить полный список форматов каждого видео (именно это на реальных
    плейлистах утыкалось в 120-секундный таймаут при обычном dump-single-json).

    Порядок попыток — как в run_ytdlp_json_ex(), см. _first_attempt_cookies():
    с cookies первой попыткой, если автоподтяжка из браузера включена, иначе
    без них первой попыткой; вторая попытка всегда в противоположную сторону
    (пропускается вовсе, если «использовать cookies» выключено — см.
    _cookies_enabled).

    Возвращает (is_playlist, info, error)."""
    t0 = time.time()
    first = _first_attempt_cookies()
    is_pl, data, err = _detect_playlist_once(url, cookies=first)
    if data is not None:
        logger.info("detect_playlist: %.0f мс, 1 попытка (cookies=%s, %s) %s",
                    (time.time() - t0) * 1000, first,
                    "плейлист" if is_pl else "видео", url)
        return is_pl, data, ""
    if not _cookies_enabled():
        return False, None, err
    is_pl2, data2, err2 = _detect_playlist_once(url, cookies=not first)
    logger.info("detect_playlist: %.0f мс, 2 попытки (%s) %s",
                (time.time() - t0) * 1000,
                "плейлист" if is_pl2 else "видео/ошибка", url)
    if data2 is not None:
        return is_pl2, data2, ""
    return False, None, err2 or err


def resolve_video_or_playlist(url):
    """Заменяет связку detect_playlist() + run_ytdlp_json_ex(), которую сейчас
    подряд делают все три инструмента (youtube.py/compress.py/trim.py) ради
    одной и той же ссылки. Для одиночного видео --flat-playlist на практике
    отдаёт те же данные целиком, включая formats, что и обычный
    dump-single-json — проверено эмпирически на YouTube и TikTok (полное
    совпадение количества и id форматов), а не только на факте наличия хоть
    каких-то данных. Если это подтверждается по факту (formats непустые) —
    второй вызов не делается вовсе. Если после flat-вызова formats
    пустые/отсутствуют (другой экстрактор мог повести себя иначе — лог ниже
    как раз для отслеживания этого на практике) — довыполняется обычный
    run_ytdlp_json_ex(), как было раньше.

    Возвращает (is_playlist, data, error):
      is_playlist=True  -> data = flat dict с entries (как раньше у detect_playlist)
      is_playlist=False -> data = ПОЛНЫЙ info dict (как раньше у run_ytdlp_json_ex)
    """
    is_pl, flat, err = detect_playlist(url)
    if err:
        return False, None, err
    if is_pl:
        return True, flat, ""

    n_formats = len(flat.get("formats") or []) if isinstance(flat, dict) else 0
    if n_formats:
        logger.info("resolve_video_or_playlist: без второго вызова, "
                    "%d форматов из flat-playlist, %s", n_formats, url)
        return False, flat, ""

    logger.info("resolve_video_or_playlist: flat без formats — "
                "довыполняю run_ytdlp_json_ex, %s", url)
    info, err2 = run_ytdlp_json_ex(url)
    if not info:
        return False, None, err2 or "Не удалось получить информацию."
    return False, info, ""


def run_ytdlp_download(build_cmd, job, work_dir, on_line,
                       err_needle=("error", "warning", "ffmpeg"), tail_len=400):
    """Скачивание через yt-dlp (Popen, построчный прогресс) с cookies «по мере
    необходимости»: первая попытка — без них, и только если она провалилась
    (и это не отмена) — повтор с cookies (см. ytdlp_cmd()). Частичные файлы
    первой попытки перед повтором удаляются, прогресс/ошибка job сбрасываются.
    Если «использовать cookies» выключено — только первая попытка: повтор с
    cookies был бы идентичным (см. _cookies_enabled).

    build_cmd(cookies) -> готовая команда (обычно
    ``core.ytdlp_cmd(*args, cookies=cookies)``).
    on_line(line, job) -> True, если строка распознана как прогресс (иначе
    строка — кандидат в err_tail, если содержит один из err_needle).

    Возвращает (rc, err_tail); rc is None, если задачу отменили."""
    err_tail = []
    rc = None
    attempts = (False, True) if _cookies_enabled() else (False,)
    for cookies in attempts:
        if cookies:
            for p in Path(work_dir).iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                except Exception:
                    pass
            err_tail = []
            with JOBS_LOCK:
                job["progress"] = 0.0
                job["error"] = ""

        proc = subprocess.Popen(
            build_cmd(cookies), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=_NO_WINDOW,
        )
        with JOBS_LOCK:
            job["proc"] = proc

        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            if not on_line(line, job):
                low = line.lower()
                if any(s in low for s in err_needle):
                    err_tail.append(line[-tail_len:])
            if job["status"] == "canceled":
                try:
                    proc.terminate()
                except Exception:
                    pass
                break

        rc = proc.wait()
        if job["status"] == "canceled":
            return None, err_tail
        if rc == 0:
            return 0, err_tail
        if cookies:
            return rc, err_tail
        # Первая попытка (без cookies) провалилась — пробуем вторую, с cookies.
    return rc, err_tail


# --- Установка зависимостей в bin/ --------------------------------------------
# Скачивание недостающих бинарников (yt-dlp/ffmpeg+ffprobe/deno) с GitHub
# Releases прямо в BIN_DIR — «вариант 1» окна установки (см. этап 2). Работает
# через ту же систему задач JOBS, что и скачивание видео — прогресс и отмена
# (POST /api/cancel/<job_id>) переиспользуются как есть, без изменений в них.

_RELEASE_REPOS = {
    "yt-dlp": "yt-dlp/yt-dlp",
    "ffmpeg": "BtbN/FFmpeg-Builds",   # тот же релиз даёт и ffmpeg.exe, и ffprobe.exe
    "deno": "denoland/deno",
}

# Порядок скачивания — качаем ПОСЛЕДОВАТЕЛЬНО (не параллельно), чтобы пиковое
# место на диске определялось самой тяжёлой операцией (ffmpeg), а не суммой
# всех сразу; ffmpeg — последним, чтобы к моменту самого долгого шага
# остальное уже было готово.
INSTALL_ORDER = ("yt-dlp", "deno", "ffmpeg")


def release_page_url(name):
    """Страница релизов на GitHub для варианта 2 (скачать вручную)."""
    repo = _RELEASE_REPOS.get(name, name)
    return f"https://github.com/{repo}/releases/latest"


def _github_latest_release(repo):
    """JSON последнего релиза репозитория (dict) или None при ошибке сети."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "jade.tools"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("GitHub releases (%s): не удалось получить релиз: %s", repo, e)
        return None


def _find_asset(release, asset_name):
    """Ассет релиза с ТОЧНЫМ именем файла (например, чтобы не подхватить
    ffmpeg-...-shared.zip вместо static-сборки), или None."""
    for a in (release or {}).get("assets") or []:
        if a.get("name") == asset_name:
            return a
    return None


def _fmt_eta(seconds):
    """123.4 -> '2:03' / '1:02:03'. Пустая строка, если не посчитать (нет
    ещё данных о скорости)."""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _download_with_progress(url, dest_path, job, base=0.0, span=100.0):
    """Скачивает url в dest_path кусками по 256 КБ потоковой записью на диск
    (весь файл разом в память не грузится), обновляя job['progress'] в
    диапазоне [base, base+span] по мере получения байт (Content-Length), а
    также job['speed'], job['eta'], job['downloaded_mb']/['total_mb'] — не
    чаще раза в 0.5с, чтобы не считать скорость по каждому куску 256 КБ.
    Проверяет отмену (job['status']=='canceled') на каждом куске — этого
    достаточно, чтобы POST /api/cancel/<job_id> сработал без специальной
    поддержки скачивания (в отличие от subprocess, тут нет job['proc'] для
    terminate(), поэтому останавливаемся сами). Возвращает "" при успехе, иначе
    текст ошибки; на отмену/ошибку недокачанный файл удаляется."""
    t0 = time.monotonic()
    done = 0
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jade.tools"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            job["total_mb"] = total / (1024 * 1024)
            last_update = 0.0
            with open(dest_path, "wb") as f:
                while True:
                    if job.get("status") == "canceled":
                        return "Отменено."
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        job["progress"] = base + span * min(1.0, done / total)
                    now = time.monotonic()
                    if now - last_update >= 0.5:
                        last_update = now
                        elapsed = now - t0
                        job["downloaded_mb"] = done / (1024 * 1024)
                        if elapsed > 0.2:
                            speed_bps = done / elapsed
                            job["speed"] = f"{speed_bps / (1024 * 1024):.1f} МБ/с"
                            if total and speed_bps > 0:
                                job["eta"] = _fmt_eta((total - done) / speed_bps)
        job["downloaded_mb"] = done / (1024 * 1024)
        job["eta"] = ""
        elapsed = time.monotonic() - t0
        avg_speed = (done / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
        logger.info("Установка зависимостей: скачано %s (%.1f МБ за %.1fс, ~%.1f МБ/с)",
                    url, done / (1024 * 1024), elapsed, avg_speed)
        return ""
    except Exception as e:
        return str(e)
    finally:
        if job.get("status") == "canceled":
            Path(dest_path).unlink(missing_ok=True)


def _extract_zip_members(zip_path, wanted, dest_dir):
    """Извлекает из zip ТОЛЬКО файлы, чей basename (без учёта регистра и пути
    внутри архива — устойчиво к смене имени папки верхнего уровня между
    релизами) есть в `wanted` (basename_lower -> итоговое имя), БЕЗ полной
    распаковки остального архива. Каждый файл сначала пишется как `<имя>.part`
    и переименовывается в финальное имя только после полного копирования —
    частичная ошибка не портит уже рабочий файл в dest_dir. Возвращает
    множество итоговых имён, которые реально нашлись и были помещены."""
    dest_dir = Path(dest_dir)
    moved = set()
    with zipfile.ZipFile(zip_path) as zf:
        remaining = dict(wanted)
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1].lower()
            if base not in remaining:
                continue
            out_name = remaining.pop(base)
            tmp_out = dest_dir / (out_name + ".part")
            with zf.open(member) as src, open(tmp_out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            tmp_out.replace(dest_dir / out_name)
            moved.add(out_name)
            if not remaining:
                break
    return moved


def _install_ytdlp(job, base, span, work_dir):
    repo = _RELEASE_REPOS["yt-dlp"]
    release = _github_latest_release(repo)
    if not release:
        return f"Не удалось получить список релизов yt-dlp. Скачайте вручную: {release_page_url('yt-dlp')}"
    asset = _find_asset(release, "yt-dlp.exe")
    if not asset:
        return f"Файл yt-dlp.exe не найден в последнем релизе. Скачайте вручную: {release_page_url('yt-dlp')}"
    tmp = Path(work_dir) / "yt-dlp.exe"
    err = _download_with_progress(asset["browser_download_url"], str(tmp), job, base, span)
    if err:
        return err
    shutil.move(str(tmp), str(BIN_DIR / "yt-dlp.exe"))
    job["progress"] = base + span
    return ""


def _install_deno(job, base, span, work_dir):
    repo = _RELEASE_REPOS["deno"]
    release = _github_latest_release(repo)
    if not release:
        return f"Не удалось получить список релизов Deno. Скачайте вручную: {release_page_url('deno')}"
    asset_name = "deno-x86_64-pc-windows-msvc.zip"
    asset = _find_asset(release, asset_name)
    if not asset:
        return f"Файл {asset_name} не найден в последнем релизе. Скачайте вручную: {release_page_url('deno')}"
    zip_path = Path(work_dir) / asset_name
    err = _download_with_progress(asset["browser_download_url"], str(zip_path), job, base, span * 0.9)
    if err:
        zip_path.unlink(missing_ok=True)
        return err
    job["stage"] = "Deno — извлечение deno.exe…"
    t0 = time.monotonic()
    try:
        extracted = _extract_zip_members(zip_path, {"deno.exe": "deno.exe"}, BIN_DIR)
    finally:
        zip_path.unlink(missing_ok=True)
    logger.info("Установка зависимостей: Deno — извлечение заняло %.1fс", time.monotonic() - t0)
    if "deno.exe" not in extracted:
        return "В архиве Deno не найден deno.exe — формат релиза мог измениться."
    job["progress"] = base + span
    return ""


def _install_ffmpeg(job, base, span, work_dir):
    """ffmpeg и ffprobe — два разных бинарника из ОДНОГО релиза/архива (см.
    TOOL_DEPENDENCIES): один запрос к GitHub, одна распаковка."""
    repo = _RELEASE_REPOS["ffmpeg"]
    release = _github_latest_release(repo)
    if not release:
        return f"Не удалось получить список релизов ffmpeg. Скачайте вручную: {release_page_url('ffmpeg')}"
    # Строго БЕЗ суффикса -shared: shared-сборка требует сопутствующие av*.dll
    # и без них не запускается, static (эта) — самодостаточна.
    asset_name = "ffmpeg-master-latest-win64-gpl.zip"
    asset = _find_asset(release, asset_name)
    if not asset:
        return f"Файл {asset_name} не найден в последнем релизе. Скачайте вручную: {release_page_url('ffmpeg')}"
    zip_path = Path(work_dir) / asset_name
    err = _download_with_progress(asset["browser_download_url"], str(zip_path), job, base, span * 0.85)
    if err:
        zip_path.unlink(missing_ok=True)
        return err
    # НЕ распаковываем весь архив (~427 МБ, плюс лишний ffplay.exe) — только
    # эти два файла, откуда бы внутри архива они ни лежали.
    job["stage"] = "ffmpeg — точечное извлечение ffmpeg.exe/ffprobe.exe…"
    t0 = time.monotonic()
    try:
        extracted = _extract_zip_members(
            zip_path, {"ffmpeg.exe": "ffmpeg.exe", "ffprobe.exe": "ffprobe.exe"}, BIN_DIR)
    finally:
        zip_path.unlink(missing_ok=True)
    logger.info("Установка зависимостей: ffmpeg — извлечение заняло %.1fс", time.monotonic() - t0)
    missing = {"ffmpeg.exe", "ffprobe.exe"} - extracted
    if missing:
        return (f"В архиве ffmpeg не найдены: {', '.join(sorted(missing))} — "
                f"формат релиза мог измениться.")
    job["progress"] = base + span
    return ""


_INSTALLERS = {"yt-dlp": _install_ytdlp, "deno": _install_deno, "ffmpeg": _install_ffmpeg}


def _install_dependencies_thread(job_id, job, targets):
    work = job_dir(job_id)
    ordered = [t for t in INSTALL_ORDER if t in targets]
    n = len(ordered) or 1
    span = 100.0 / n
    try:
        for i, name in enumerate(ordered):
            if job["status"] == "canceled":
                break
            base = i * span
            job["progress"] = base
            job["stage"] = f"{_DEP_LABELS.get(name, name)} — скачивание ({i + 1} из {n})…"
            job["speed"] = ""
            job["eta"] = ""
            job["downloaded_mb"] = 0.0
            job["total_mb"] = 0.0
            err = _INSTALLERS[name](job, base, span, work)
            if job["status"] == "canceled":
                break
            if err:
                job["status"] = "error"
                job["error"] = f"{_DEP_LABELS.get(name, name)}: {err}"
                logger.error("Установка зависимостей: %s — %s", name, err)
                return
        if job["status"] == "canceled":
            logger.info("Установка зависимостей: отменено пользователем")
            return
        refresh_dependencies()
        if "yt-dlp" in ordered:
            _ytdlp_update_cache.update(ts=0.0, result=None)
        job["progress"] = 100.0
        job["stage"] = "Готово"
        job["status"] = "done"
        logger.info("Установка зависимостей: готово (%s)", ", ".join(ordered))
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        logger.error("Установка зависимостей: исключение: %s", e)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def active_install_job():
    """(job_id, job) уже идущей задачи установки зависимостей, или (None, None).
    Вызывать под JOBS_LOCK."""
    for jid, job in JOBS.items():
        if job.get("kind") == "install" and job["status"] in _ACTIVE_STATUSES:
            return jid, job
    return None, None


def start_install_job(targets):
    """Запускает фоновую установку зависимостей в bin/ (окно установки,
    вариант 1). targets — подмножество {"yt-dlp","ffmpeg","deno"} (ffmpeg тянет
    за собой и ffprobe — общий релиз). Возвращает job_id или None, если после
    фильтрации целей не осталось.

    Если установка уже идёт (например, окно установки открыли повторно и
    снова нажали «начать») — НЕ запускает вторую параллельную установку (риск
    гонки при записи в bin/), а отдаёт job_id уже идущей задачи, чтобы фронт
    переподключился к её прогрессу."""
    targets = [t for t in targets if t in _INSTALLERS]
    if not targets:
        return None
    with JOBS_LOCK:
        existing_id, _ = active_install_job()
        if existing_id:
            logger.info("Установка зависимостей: уже идёт (job %s) — повторный запуск пропущен", existing_id)
            return existing_id
        cleanup_old_jobs()
        job_id = uuid.uuid4().hex[:12]
        job = new_job({"status": "downloading", "progress": 0.0, "kind": "install",
                       "title": "Установка зависимостей", "stage": "Подготовка…"})
        JOBS[job_id] = job
    t = threading.Thread(target=_install_dependencies_thread,
                         args=(job_id, job, targets), daemon=True)
    t.start()
    return job_id


# --- Проверка обновлений yt-dlp -----------------------------------------------
# YouTube меняет защиту часто, поэтому свежий yt-dlp — не косметика, а нужное
# условие работы. Сверяем локальную версию с последним релизом на GitHub.

_YTDLP_UPDATE_CACHE_TTL = 1800   # с — не дёргать GitHub API чаще этого интервала
_ytdlp_update_cache = {"ts": 0.0, "result": None}


def ytdlp_current_version():
    """Установленная версия yt-dlp (строка) или "" при ошибке."""
    try:
        proc = subprocess.run(
            [YT_DLP_BIN, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=_NO_WINDOW,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _parse_ytdlp_version(v):
    """"2026.06.09" -> (2026, 6, 9), для сравнения версий. () при ошибке разбора."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return ()


_PIP_INSTALLED_TTL = 300   # с — pip show не бесплатный, результат меняется редко
_pip_installed_cache = {"ts": 0.0, "result": None}


def _pip_launcher():
    """Первый доступный лаунчер Python с pip (`py -3` или `python`), видимый в
    PATH, или None. В собранном exe sys.executable — это сам jade.tools.exe, а
    не Python, поэтому ищем системный лаунчер, а не используем sys.executable."""
    for launcher in (["py", "-3"], ["python"]):
        if shutil.which(launcher[0]):
            return launcher
    return None


def _ytdlp_pip_installed(force=False):
    """True, если yt-dlp виден pip'у (`pip show yt-dlp` завершился успешно) —
    единственный признак, отличающий pip-копию от установленной вручную/через
    winget (см. ytdlp_source_kind: это разделение решает, как обновлять).

    Результат кэшируется на _PIP_INSTALLED_TTL секунд (OPT-3): pip show —
    внешний процесс, а вызывается это при каждом открытии настроек и при
    проверке обновлений; состояние pip за это время практически не меняется."""
    now = time.time()
    if (not force and _pip_installed_cache["result"] is not None
            and now - _pip_installed_cache["ts"] < _PIP_INSTALLED_TTL):
        return _pip_installed_cache["result"]
    result = False
    launcher = _pip_launcher()
    if launcher:
        try:
            proc = subprocess.run(
                launcher + ["-m", "pip", "show", "yt-dlp"],
                capture_output=True, timeout=20, creationflags=_NO_WINDOW,
            )
            result = proc.returncode == 0
        except Exception:
            result = False
    _pip_installed_cache.update(ts=now, result=result)
    return result


def ytdlp_source_kind():
    """Откуда взят резолвнутый yt-dlp — определяет способ обновления
    (do_ytdlp_update) и текст/доступность кнопки в настройках:
      * "bin"          — bin/yt-dlp.exe рядом с exe — обновляем сами.
      * "pip"          — системная pip-копия — pip install -U, как раньше.
      * "system-other" — в PATH, но НЕ через pip (winget/ручной exe) — не
                         трогаем, кнопка неактивна («обновите вручную»).
      * "missing"      — не резолвился вовсе (см. dependency_status)."""
    status = dependency_status().get("yt-dlp", {})
    if not status.get("found"):
        return "missing"
    if status.get("source") == "bin":
        return "bin"
    return "pip" if _ytdlp_pip_installed() else "system-other"


def check_ytdlp_update(force=False):
    """Сравнить установленную версию yt-dlp с последним релизом на GitHub.

    Возвращает {status: "updated"|"available"|"error", current, latest,
    message, source_kind}. Результат кэшируется на _YTDLP_UPDATE_CACHE_TTL
    секунд (если force=False), чтобы не дёргать GitHub при каждом открытии
    главной страницы — только ручная проверка из настроек (force=True)
    обходит кэш."""
    now = time.time()
    cached = _ytdlp_update_cache["result"]
    if not force and cached and now - _ytdlp_update_cache["ts"] < _YTDLP_UPDATE_CACHE_TTL:
        return cached

    source_kind = ytdlp_source_kind()

    current = ytdlp_current_version()
    if not current:
        result = {"status": "error", "current": "", "latest": "",
                  "message": "Не удалось определить установленную версию yt-dlp.",
                  "source_kind": source_kind}
        _ytdlp_update_cache.update(ts=now, result=result)
        return result

    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"User-Agent": "jade.tools"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("tag_name") or "").lstrip("v").strip()
    except Exception as e:
        result = {"status": "error", "current": current, "latest": "",
                  "message": f"Не удалось проверить обновление: {e}",
                  "source_kind": source_kind}
        _ytdlp_update_cache.update(ts=now, result=result)
        logger.warning("Проверка обновления yt-dlp не удалась: %s", e)
        return result

    if not latest:
        result = {"status": "error", "current": current, "latest": "",
                  "message": "Не удалось получить данные о последней версии yt-dlp.",
                  "source_kind": source_kind}
        _ytdlp_update_cache.update(ts=now, result=result)
        return result

    cur_t, lat_t = _parse_ytdlp_version(current), _parse_ytdlp_version(latest)
    if cur_t and lat_t and cur_t < lat_t:
        result = {"status": "available", "current": current, "latest": latest,
                  "message": f"Доступна новая версия yt-dlp: {latest} (у вас {current}).",
                  "source_kind": source_kind}
        logger.info("yt-dlp: доступно обновление %s -> %s", current, latest)
    else:
        result = {"status": "updated", "current": current, "latest": latest,
                  "message": f"yt-dlp обновлён (версия {current}).",
                  "source_kind": source_kind}
        logger.info("yt-dlp: версия актуальна (%s)", current)
    _ytdlp_update_cache.update(ts=now, result=result)
    return result


def _update_ytdlp_pip():
    """Обновление системной pip-копии: `pip install -U yt-dlp` — путь,
    работавший до этапа 2, оставлен без изменений."""
    proc = None
    for launcher in (["py", "-3"], ["python"]):
        try:
            proc = subprocess.run(
                launcher + ["-m", "pip", "install", "--upgrade", "yt-dlp"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120, creationflags=_NO_WINDOW,
            )
            break
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.error("Обновление yt-dlp: превышено время ожидания pip")
            return {"status": "error", "current": "", "latest": "",
                    "message": "Превышено время ожидания pip при обновлении yt-dlp."}

    if proc is None:
        logger.error("Обновление yt-dlp: не найден лаунчер Python (py/python)")
        return {"status": "error", "current": "", "latest": "",
                "message": ("Не найден Python в PATH — обновите вручную: "
                            "py -m pip install -U yt-dlp.")}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        logger.error("Обновление yt-dlp не удалось: %s", err[:500])
        last = [ln for ln in err.splitlines() if ln.strip()]
        return {"status": "error", "current": ytdlp_current_version(), "latest": "",
                "message": "Не удалось обновить yt-dlp: " +
                          (last[-1][:300] if last else "неизвестная ошибка pip.")}

    logger.info("yt-dlp обновлён через pip: %s", proc.stdout.strip()[-300:])
    _ytdlp_update_cache.update(ts=0.0, result=None)
    return check_ytdlp_update(force=True)


def _update_ytdlp_bin():
    """Самообновление bin/-копии: сперва встроенный self-update yt-dlp (`-U`,
    работает для standalone-релизов с GitHub — yt-dlp сам себя перезаписывает
    на месте), при неудаче — перекачка yt-dlp.exe напрямую с GitHub Releases
    (тот же путь, что и при первичной установке, см. _install_ytdlp)."""
    try:
        proc = subprocess.run(
            [YT_DLP_BIN, "-U"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=_NO_WINDOW,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and "error" not in out.lower():
            logger.info("yt-dlp обновлён через self-update (-U): %s", out.strip()[-300:])
            _ytdlp_update_cache.update(ts=0.0, result=None)
            return check_ytdlp_update(force=True)
        logger.warning("yt-dlp -U не удался (код %s): %s", proc.returncode, out.strip()[-300:])
    except Exception as e:
        logger.warning("yt-dlp -U не удался (%s), перекачиваю с GitHub", e)

    work = job_dir(f"ytdlp-update-{uuid.uuid4().hex[:8]}")
    dummy_job = {"status": "downloading", "progress": 0.0}
    try:
        err = _install_ytdlp(dummy_job, 0.0, 100.0, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if err:
        return {"status": "error", "current": ytdlp_current_version(), "latest": "",
                "message": f"Не удалось обновить yt-dlp: {err}"}
    refresh_dependencies()
    _ytdlp_update_cache.update(ts=0.0, result=None)
    return check_ytdlp_update(force=True)


def do_ytdlp_update():
    """Обновить yt-dlp — способ зависит от источника (см. ytdlp_source_kind):
    bin/-копия обновляется приложением само, системная pip-копия — как раньше
    (pip install -U), а установленная в системе НЕ через pip (winget, ручной
    exe в PATH) не трогается вовсе — вызывающая сторона должна сама скрыть/
    задизейблить кнопку для этого случая («обновите вручную»).

    Возвращает тот же формат, что check_ytdlp_update:
    {status: "updated"|"available"|"error", current, latest, message, source_kind}."""
    kind = ytdlp_source_kind()
    if kind == "bin":
        result = _update_ytdlp_bin()
    elif kind == "pip":
        result = _update_ytdlp_pip()
    else:
        result = {"status": "error", "current": ytdlp_current_version(), "latest": "",
                  "message": ("yt-dlp установлен в системе не через pip — "
                              "обновите вручную (см. настройки).")}
    result["source_kind"] = kind
    return result


# --- Имена файлов -------------------------------------------------------------

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name, fallback="media", max_len=150):
    """Безопасный компонент имени файла для Windows: убирает только символы,
    недопустимые в путях (``<>:"/\\|?*`` и управляющие), сохраняя юникод.

    В отличие от werkzeug.utils.secure_filename, который стирает вообще все
    не-ASCII символы (в т.ч. кириллицу) — из-за этого названия видео на
    русском превращались в пустую строку, и итоговый файл получал общее имя
    ("media"/"video") вместо настоящего названия."""
    name = _INVALID_FS_CHARS.sub("", str(name or "")).strip(" .")
    return name[:max_len] or fallback


_SAFE_EXT_RE = re.compile(r'^\.[A-Za-z0-9]{1,6}$')


def safe_ext(ext, default=".mp4"):
    """Безопасное расширение файла для имени на диске: только '.<алфанум 1-6>',
    иначе default. Имя загрузки приходит от клиента, поэтому ':' (ADS в NTFS),
    '*', '?', '"' и прочее недопустимое в путях Windows не пропускаем
    (см. REL-6). Расширение не может содержать разделитель пути — это уже
    гарантирует os.path.splitext, здесь закрываем остальные спецсимволы."""
    e = str(ext or "")
    return e if _SAFE_EXT_RE.match(e) else default


# --- ffmpeg / ffprobe --------------------------------------------------------

_FF_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_FF_TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffprobe_duration(path):
    """Длительность файла в секундах (float). 0.0, если определить не удалось."""
    # Сначала пробуем ffprobe (точнее), затем — ffmpeg как запасной вариант.
    try:
        proc = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=_NO_WINDOW,
        )
        return float(proc.stdout.strip())
    except Exception:
        pass
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=_NO_WINDOW,
        )
        m = _FF_DUR_RE.search(proc.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def ffprobe_resolution(path):
    """(width, height) первого видеопотока, или (0, 0)."""
    try:
        proc = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=_NO_WINDOW,
        )
        w, h = proc.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 0, 0


def _is_progress_field(line):
    """True, если строка похожа на key=value поле из вывода ffmpeg -progress."""
    if "=" not in line:
        return False
    key = line.split("=", 1)[0]
    return bool(key) and " " not in key and key.replace("_", "").isalnum()


def run_ffmpeg_progress(args, job, duration, base=0.0, span=100.0):
    """Запускает ffmpeg c машинно-читаемым прогрессом, обновляя job['progress'].

    args      — аргументы после общих флагов (вкл. -i и выходной файл);
    duration  — длительность видео (с) для пересчёта прогресса в проценты;
    base/span — отображение прогресса прогона в общий диапазон (для 2 проходов:
                первый base=0 span=50, второй base=50 span=50).

    Возвращает (returncode, err_tail). Уважает отмену (job['status']=='canceled').
    """
    cmd = ([FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
            "-nostats", "-progress", "pipe:1"] + list(args))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1, creationflags=_NO_WINDOW,
    )
    with JOBS_LOCK:
        job["proc"] = proc

    err_tail = []
    for raw in proc.stdout:
        line = raw.strip()
        m = _FF_TIME_RE.search(line)
        if m and duration > 0:
            h, mi, s = m.groups()
            t = int(h) * 3600 + int(mi) * 60 + float(s)
            pct = max(0.0, min(1.0, t / duration))
            job["progress"] = base + pct * span
        elif line and not _is_progress_field(line):
            # Не-прогресс строки — потенциальные ошибки ffmpeg.
            err_tail.append(line[-300:])
        if job["status"] == "canceled":
            try:
                proc.terminate()
            except Exception:
                pass
            break

    rc = proc.wait()
    if rc != 0 and job.get("status") != "canceled":
        logger.warning("ffmpeg завершился с кодом %s: %s", rc, "; ".join(err_tail[-3:]))
    return rc, err_tail


# --- Статистика и очистка (для страницы настроек) ----------------------------

def path_stats(path):
    """Размер (байт), время изменения и обращения для файла/папки.

    Возвращает {exists, size, mtime, atime}. Для папки size — суммарный,
    mtime/atime — самые свежие среди содержимого."""
    p = Path(path)
    if not p.exists():
        return {"exists": False, "size": 0, "mtime": None, "atime": None}
    try:
        if p.is_dir():
            size = 0
            st = p.stat()
            mtime, atime = st.st_mtime, st.st_atime
            for f in p.rglob("*"):
                try:
                    fst = f.stat()
                    if f.is_file():
                        size += fst.st_size
                    mtime = max(mtime, fst.st_mtime)
                    atime = max(atime, fst.st_atime)
                except Exception:
                    pass
            return {"exists": True, "size": size, "mtime": mtime, "atime": atime}
        st = p.stat()
        return {"exists": True, "size": st.st_size,
                "mtime": st.st_mtime, "atime": st.st_atime}
    except Exception:
        return {"exists": False, "size": 0, "mtime": None, "atime": None}


def clear_path(path, name_filter=None):
    """Очистить содержимое папки (или удалить файл). True при успехе.

    name_filter — необязательный предикат по имени элемента: если задан,
    удаляются ТОЛЬКО совпадающие элементы. Нужен для temp/cache: если путь
    указывает на пользовательскую папку, очистка не должна сносить чужое
    (см. REL-1) — приложение владеет лишь каталогами со своими именами."""
    p = Path(path)
    try:
        if p.is_dir():
            for e in p.iterdir():
                if name_filter is not None and not name_filter(e.name):
                    continue
                if e.is_dir():
                    shutil.rmtree(e, ignore_errors=True)
                else:
                    try:
                        e.unlink()
                    except Exception:
                        pass
        elif p.is_file():
            p.unlink()
        return True
    except Exception:
        pass
    return False


# --- Место на диске / размер кэша+temp (для баннера на главной) --------------

DISK_FREE_WARN_BYTES = 2 * 1024 ** 3       # предупреждать, если свободно < 2 ГБ
CACHE_TEMP_WARN_BYTES = 5 * 1024 ** 3      # предупреждать, если кэш+temp > 5 ГБ
_DISK_CHECK_TTL = 60                        # с — не пересчитывать размер папок чаще
_disk_check_cache = {"ts": 0.0, "result": None}


def _fmt_bytes(n):
    if not n:
        return "0 Б"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} {units[i]}"


def check_disk_and_cache(force=False):
    """Свободное место на диске + суммарный размер кэша/temp jade.tools.

    Порог — DISK_FREE_WARN_BYTES / CACHE_TEMP_WARN_BYTES. Кэшируется
    _DISK_CHECK_TTL секунд (обход папки кэша рекурсивный — не бесплатный)."""
    now = time.time()
    cached = _disk_check_cache["result"]
    if not force and cached and now - _disk_check_cache["ts"] < _DISK_CHECK_TTL:
        return cached

    try:
        free = shutil.disk_usage(str(DOWNLOADS_DIR)).free
    except Exception:
        free = None
    temp_size = path_stats(DOWNLOADS_DIR).get("size", 0)
    cache_size = path_stats(CACHE_DIR).get("size", 0)
    total = temp_size + cache_size

    reasons = []
    warning = False
    if free is not None and free < DISK_FREE_WARN_BYTES:
        warning = True
        reasons.append(f"на диске свободно всего {_fmt_bytes(free)}")
    if total > CACHE_TEMP_WARN_BYTES:
        warning = True
        reasons.append(f"кэш и временные файлы jade.tools занимают {_fmt_bytes(total)}")

    message = ("Заканчивается место на диске: " + "; ".join(reasons) +
               ". Рекомендуем очистить кэш и временные файлы в настройках."
               ) if warning else ""

    result = {
        "warning": warning, "message": message,
        "free_bytes": free, "temp_bytes": temp_size,
        "cache_bytes": cache_size, "total_bytes": total,
        "free_fmt": _fmt_bytes(free) if free is not None else "?",
        "total_fmt": _fmt_bytes(total),
    }
    _disk_check_cache.update(ts=now, result=result)
    if warning:
        logger.warning("Предупреждение о месте на диске: %s", message)
    return result
