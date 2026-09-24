# -*- coding: utf-8 -*-
"""Локальный мультитул — ядро приложения.

Flask-сервер, который:
  * отдаёт лендинг со списком инструментов;
  * регистрирует blueprint'ы инструментов из пакета tools/;
  * предоставляет общие роуты статуса/отмены/отдачи файла, которыми
    пользуются все инструменты через единую систему задач (core.JOBS).

Запуск:
    py app.py
Затем открыть http://127.0.0.1:5000
"""

import os
import random
import signal
import threading
from urllib.parse import urlparse

from flask import Flask, render_template, jsonify, send_file, abort, request, redirect, url_for

import core
import tools

app = Flask(__name__)

# Список инструментов доступен во всех шаблонах (для лендинга и сайдбара).
@app.context_processor
def _inject_tools():
    return {"tools": tools.tools()}


# Тема — во всех шаблонах сразу, чтобы _base.html мог поставить data-theme
# на <html> при самом первом рендере (без вспышки неверной темы до JS).
@app.context_processor
def _inject_theme():
    return {"theme": core.get_settings().get("theme", "dark")}


# Настройки — во всех шаблонах сразу, чтобы страницы инструментов могли
# подставить сохранённые предпочтения (формат видео/звука, битрейт/размер
# сжатия и т.д.) как значения по умолчанию при первом рендере, без лишнего
# fetch (см. youtube.html/compress.html/trim.html).
@app.context_processor
def _inject_settings():
    return {"settings": core.get_settings()}


# Запуск из временной папки (часть 2, см. onedir_and_startup_checks.md) — во
# всех шаблонах сразу, чтобы блокирующая модалка (_base.html) появлялась
# независимо от того, с какой страницы открылось приложение.
@app.context_processor
def _inject_temp_launch():
    return {"running_from_temp": core.RUNNING_FROM_TEMP}


# Регистрируем все инструменты.
for _bp in tools.blueprints():
    app.register_blueprint(_bp)

# Страница настроек — отдельный раздел (не инструмент-плитка на лендинге).
import tools.settings as _settings_tool
app.register_blueprint(_settings_tool.bp)

# Отдельная страница вне общей навигации (не инструмент-плитка) — см. tools/rq7.py.
import tools.rq7 as _rq7_page
app.register_blueprint(_rq7_page.bp)


# --- Защита POST от чужих сайтов (CSRF/DNS-rebinding, SEC-3) -----------------
# Приложение локальное и без аутентификации, поэтому state-changing запрос с
# произвольного сайта (обычная HTML-форма) или через DNS-rebinding проходить
# не должен. Браузерные запросы с чужого origin отсекаются по Origin/
# Sec-Fetch-Site, чужой Host — по allowlist. Клиенты без этих заголовков
# (curl, скрипты) не блокируются, чтобы не мешать ручной работе.
_ENV_HOST = (os.environ.get("YTD_HOST") or "").strip().lower()
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
if _ENV_HOST and _ENV_HOST not in ("0.0.0.0", "::"):
    _ALLOWED_HOSTNAMES.add(_ENV_HOST)
# При бинде на все интерфейсы (0.0.0.0) заранее неизвестно, по какому имени
# придёт браузер, — Host-allowlist тогда не применяем (Origin/Sec-Fetch-Site
# продолжают действовать).
_HOST_CHECK_ENABLED = _ENV_HOST != "0.0.0.0"


def _hostname_of(value):
    """Имя хоста без схемы/порта ('' — если разобрать не удалось).

    Принимает и Host ('127.0.0.1:5000'), и Origin ('http://localhost:5000')."""
    if not value:
        return ""
    raw = value.strip()
    if "://" in raw:
        raw = urlparse(raw).netloc or ""
    raw = raw.rsplit("@", 1)[-1]           # userinfo, на всякий случай
    if raw.startswith("["):                # IPv6: [::1]:5000
        return raw[1:].split("]", 1)[0].lower()
    return raw.split(":", 1)[0].lower()


@app.before_request
def _block_foreign_requests():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if _HOST_CHECK_ENABLED:
        host = _hostname_of(request.headers.get("Host"))
        if host and host not in _ALLOWED_HOSTNAMES:
            return jsonify({"error": "Запрос с недопустимого Host отклонён."}), 403
    if (request.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return jsonify({"error": "Запрос с постороннего сайта отклонён."}), 403
    origin = request.headers.get("Origin")
    if origin and _hostname_of(origin) not in _ALLOWED_HOSTNAMES:
        return jsonify({"error": "Запрос с постороннего сайта отклонён."}), 403
    return None


# При каждом переходе между страницами инструментов (в любую сторону) — шанс
# 1 из 50 попасть на _rq7_page вместо запрошенного инструмента. Секретность —
# см. tools/rq7.py и templates/rq7.html: сама вероятность не секрет, но её
# нет смысла тратить на переходы не между инструментами (лендинг/настройки).
# После core.get_settings()["seen"] == True — не срабатывает никогда.
@app.before_request
def _maybe_tool_transition_easter_egg():
    if request.method != "GET":
        return None
    tool_paths = {t["url"] for t in tools.tools()}
    if request.path not in tool_paths:
        return None
    if core.get_settings().get("seen"):
        return None
    ref_path = urlparse(request.referrer or "").path
    if ref_path not in tool_paths or ref_path == request.path:
        return None
    if random.random() < 1 / 50:
        return redirect(url_for("rq7.page"))
    return None

# Фоновый уборщик downloads/: чистит остатки прошлых запусков и удаляет
# отлежавшиеся задачи. Запускаем на уровне модуля, чтобы работало и при
# `py app.py`, и при запуске из трея (tray_app импортирует этот модуль).
core.start_janitor()

# Смена версии между запусками — сравнение с last_seen_version и запись
# текущей версии как увиденной (changelog пока не показываем, см. докстринг
# core.mark_version_seen). Синхронно (не сеть, просто сравнение строк) — не
# задерживает старт.
core.check_version_change_at_startup()

# Проверка новых релизов на GitHub — в фоновом потоке, чтобы не задерживать
# старт сервера и открытие браузера (см. core.start_app_update_check).
core.start_app_update_check()


# --- Лендинг -----------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", version=_settings_tool.VERSION)


# --- Фоновые проверки для главной страницы (обновление yt-dlp, место на диске) ---

@app.route("/api/ytdlp_update_check")
def api_ytdlp_update_check():
    """Автоматическая проверка при открытии главной страницы (кэшируется на
    стороне core.check_ytdlp_update, чтобы не дёргать GitHub на каждый заход)."""
    return jsonify(core.check_ytdlp_update())


@app.route("/api/app_update_check")
def api_app_update_check():
    """Плашка «доступна новая версия» у кнопки GitHub (_base.html, все
    страницы). Результат уже посчитан фоновым потоком при старте
    (core.start_app_update_check) и лежит в кэше check_app_update — обычный
    запрос почти всегда просто читает его, не дёргая GitHub заново."""
    return jsonify(core.check_app_update())


@app.route("/api/ytdlp_update_do", methods=["POST"])
def api_ytdlp_update_do():
    """Кнопка «обновить» в тосте на главной — реальное обновление (не только
    проверка версии); способ зависит от источника yt-dlp, см. core.do_ytdlp_update."""
    return jsonify(core.do_ytdlp_update())


@app.route("/api/disk_check")
def api_disk_check():
    """Свободное место на диске / суммарный размер кэша+temp — для баннера
    на главной странице."""
    return jsonify(core.check_disk_and_cache())


@app.route("/api/paths_status")
def api_paths_status():
    """Доступность temp/cache (этап 4) — для блокирующей модалки/баннера на
    главной странице и пульсации строк на странице настроек. Дёргается при
    каждом открытии главной и периодически со страницы настроек, пока она
    открыта (см. соответствующие шаблоны) — дешёвая проверка (os.access),
    без обхода файловой системы."""
    return jsonify(core.paths_status())


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Завершить приложение целиком (кнопка блокирующей модалки запуска из
    временной папки, часть 2) — тот же эффект, что «Выход» из трея. Ответ
    сначала уходит браузеру, процесс завершается с небольшой задержкой в
    отдельном потоке."""
    def _do_quit():
        import time
        time.sleep(0.3)
        try:
            core.cleanup_all_jobs()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_do_quit, daemon=True).start()
    return jsonify({"ok": True})


# --- Установка зависимостей (общее для всех инструментов + настроек) ---------
# Окно установки (этап 2) переиспользуется на любой странице инструмента,
# когда missing_dependencies_payload() сигналит need_install, и на странице
# настроек (кнопка «переустановить») — поэтому роуты общие, а не в tools/*.py.

@app.route("/api/deps/info")
def api_deps_info():
    """Статус/ссылки/команды по группам зависимостей (yt-dlp, ffmpeg+ffprobe,
    Deno) — для окна установки (варианты 2/3) и раздела настроек.
    active_job — id уже идущей автоустановки, если она есть: окно установки
    при открытии переподключается к её прогрессу вместо показа кнопки «начать»
    (защита от повторного параллельного запуска — см. core.start_install_job)."""
    with core.JOBS_LOCK:
        active_job, _ = core.active_install_job()
    return jsonify({"bin_dir": str(core.BIN_DIR), "groups": core.dependency_groups(),
                    "active_job": active_job})


@app.route("/api/deps/install", methods=["POST"])
def api_deps_install():
    """Вариант 1 окна установки: скачать выбранные группы зависимостей в bin/.
    Прогресс/отмена — через уже существующие /api/status и /api/cancel (эта
    задача живёт в той же core.JOBS, что и скачивание видео)."""
    data = request.get_json(silent=True) or {}
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        return jsonify({"error": "Не указано, что устанавливать."}), 400
    job_id = core.start_install_job(targets)
    if not job_id:
        return jsonify({"error": "Неизвестные зависимости."}), 400
    return jsonify({"ok": True, "job_id": job_id})


# --- Общие роуты задач (для всех инструментов) -------------------------------

@app.route("/api/status/<job_id>")
def api_status(job_id):
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "status": job["status"],
            "progress": round(job["progress"], 1),
            "speed": job["speed"],
            "eta": job["eta"],
            "downloaded_mb": round(job["downloaded_mb"], 1),
            "total_mb": round(job["total_mb"], 1),
            "stage": job.get("stage", ""),
            "title": job["title"],
            "download_name": job["download_name"],
            "error": job["error"],
            "result_size_mb": job.get("result_size_mb"),
        })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        if job["status"] in ("downloading", "processing", "pending"):
            job["status"] = "canceled"
            proc = job.get("proc")
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
        return jsonify({"ok": True})


@app.route("/api/file/<job_id>")
def api_file(job_id):
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or job["status"] != "done" or not job["filename"]:
            abort(404)
        path = job["filename"]
        dl_name = job["download_name"]

    if not os.path.isfile(path):
        abort(404)
    if core.MAX_FILE_BYTES and os.path.getsize(path) > core.MAX_FILE_BYTES:
        abort(413)

    # Файл ушёл в браузер — копия в downloads/ больше не нужна: помечаем задачу
    # на удаление через небольшой запас (на случай повторного скачивания).
    with core.JOBS_LOCK:
        if job:
            core.schedule_cleanup(job)

    return send_file(path, as_attachment=True, download_name=dl_name)


# --- Запуск ------------------------------------------------------------------

def main():
    host = os.environ.get("YTD_HOST", "127.0.0.1")
    # YTD_PORT — основная переменная; PORT поддержан как стандартный фоллбэк
    # (его задают многие хостинги и dev-обёртки).
    port = int(os.environ.get("YTD_PORT") or os.environ.get("PORT") or "5000")

    signal.signal(signal.SIGINT, lambda *_: core.cleanup_all_jobs())
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda *_: core.cleanup_all_jobs())
        except Exception:
            pass

    print("=" * 60)
    print(" jade.tools запущен.")
    print(f" Откройте в браузере:  http://{host}:{port}")
    print(" Чтобы остановить — Ctrl+C.")
    print("=" * 60)
    core.logger.info("Сервер запущен: http://%s:%s", host, port)
    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    finally:
        core.cleanup_all_jobs()
        core.logger.info("Сервер остановлен.")


if __name__ == "__main__":
    main()
