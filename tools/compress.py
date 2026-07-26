# -*- coding: utf-8 -*-
"""Инструмент: сжатие видео под целевой размер (в МБ).

Источник видео — либо загруженный с компьютера файл, либо ссылка (YouTube и
другие сайты, которые поддерживает yt-dlp — тогда оригинал сначала
скачивается). Дальше считается нужный битрейт под заданный размер и делается
двухпроходное кодирование x264 — это даёт точное попадание в целевой размер.
"""

import os
import re
import uuid
import shutil
import threading
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify

import core

bp = Blueprint("compress", __name__, url_prefix="/compress")

TOOL = {
    "id": "compress",
    "name": "сжать",
    "desc": "уменьшить видео до нужного размера в МБ",
    "url": "/compress/",
    "icon": ('<svg viewBox="0 0 24 24"><path d="M8 3v3a2 2 0 0 1-2 2H3'
             'M16 3v3a2 2 0 0 0 2 2h3M8 21v-3a2 2 0 0 0-2-2H3'
             'M16 21v-3a2 2 0 0 1 2-2h3"/></svg>'),
}

_DL_RE = re.compile(r'\[download\]\s+([\d.]+)%')


def _parse_dl_progress(line, job):
    m = _DL_RE.search(line)
    if not m:
        return False
    try:
        job["progress"] = float(m.group(1))
    except ValueError:
        pass
    return True


def _download_source(job_id, job, url):
    """Скачивает исходное видео (видео+звук, mp4) для последующего сжатия.

    Возвращает Path к файлу или None (с выставленным job['status']='error'
    или 'canceled')."""
    work = core.job_dir(job_id)
    # Кэш: исходник этого ролика уже качался для сжатия — переиспользуем.
    vid = job.get("video_id") or ""
    ckey = f"{vid}|compress-src" if vid else ""
    cached = core.cache_get(ckey) if ckey else None
    if cached:
        core.logger.info("Сжать: исходник из кэша -> %s", Path(cached).name)
        job["progress"] = 100.0
        return Path(cached)

    out_tmpl = str(work / "source.%(ext)s")
    core.logger.info("Сжать: скачивание исходника начато (%s)", url)
    rc, err_tail = core.run_ytdlp_download(
        lambda cookies: core.ytdlp_cmd(
            "-o", out_tmpl, "-f", "bv*+ba/b", "--merge-output-format", "mp4",
            "--newline", "--progress", url, cookies=cookies,
        ),
        job, work, _parse_dl_progress,
        err_needle=("error", "ffmpeg"), tail_len=300,
    )
    if rc is None:
        return None
    if rc != 0:
        job["status"] = "error"
        job["error"] = (core.friendly_ytdlp_error("\n".join(err_tail))
                        if err_tail else f"yt-dlp exit code {rc}")
        return None

    files = [p for p in work.glob("source.*") if p.is_file()]
    if not files:
        job["status"] = "error"
        job["error"] = "Исходный файл не найден после скачивания."
        return None
    result = max(files, key=lambda p: p.stat().st_mtime)
    if ckey:
        core.cache_put(ckey, str(result))
    core.logger.info("Сжать: исходник скачан -> %s", result.name)
    return result


def _compress_thread(job_id, job):
    work = core.job_dir(job_id)
    core.logger.info("Сжать: начато (%s), target_mb=%s", job.get("title"), job.get("target_mb"))
    try:
        # 1. Получить исходный файл (с диска или скачать с YouTube).
        src = job.get("src_path")
        if not src:
            job["status"] = "downloading"
            job["stage"] = "Скачивание исходного видео…"
            job["progress"] = 0.0
            src = _download_source(job_id, job, job.get("url", ""))
            if src is None:
                if job["status"] == "canceled":
                    shutil.rmtree(work, ignore_errors=True)
                return
        src = Path(src)
        if not src.is_file():
            job["status"] = "error"
            job["error"] = "Исходный файл недоступен."
            return

        # 2. Длительность нужна для расчёта битрейта.
        duration = job.get("duration") or core.ffprobe_duration(src)
        if duration <= 0:
            job["status"] = "error"
            job["error"] = "Не удалось определить длительность видео."
            return

        target_mb = float(job["target_mb"])
        audio_kbps = int(job.get("audio_kbps") or 128)
        # Понижение разрешения (без апскейла, высота кратна 2).
        height = int(job.get("height") or 0)
        scale_args = (["-vf", f"scale=-2:trunc(min(ih\\,{height})/2)*2"]
                     if height > 0 else [])

        if target_mb > 0:
            # 3. Целевой битрейт: размер(МБ)*8192 кбит / длительность(с).
            #    Запас 3% на накладные расходы контейнера.
            total_kbps = (target_mb * 8192) / duration * 0.97
            video_kbps = total_kbps - audio_kbps
            if video_kbps < 50:
                job["status"] = "error"
                job["error"] = (f"Целевой размер слишком мал для длительности "
                                f"{int(duration)} с. Увеличьте размер или уменьшите "
                                f"битрейт звука.")
                return

            out = work / (src.stem.replace("source", "video") + f" [{target_mb:g}MB].mp4")
            passlog = str(work / "ffpass")
            common = (["-i", str(src), "-c:v", "libx264", "-b:v", f"{int(video_kbps)}k",
                      "-preset", "fast", "-pix_fmt", "yuv420p"] + scale_args)

            job["status"] = "processing"
            job["progress"] = 0.0

            # 4. Проход 1 — анализ (без звука, вывод в никуда). Двухпроходное
            #    кодирование нужно именно ради точного попадания в размер —
            #    без цели по размеру (ниже) оно не нужно, достаточно одного прохода.
            job["stage"] = "Проход 1 из 2 (анализ)…"
            rc, errs = core.run_ffmpeg_progress(
                common + ["-pass", "1", "-passlogfile", passlog, "-an",
                          "-f", "null", os.devnull],
                job, duration, base=0.0, span=50.0)
            if job["status"] == "canceled":
                shutil.rmtree(work, ignore_errors=True)
                return
            if rc != 0:
                job["status"] = "error"
                job["error"] = "; ".join(errs[-2:]) or f"ffmpeg pass 1 exit code {rc}"
                return

            # 5. Проход 2 — финальный файл со звуком.
            job["stage"] = "Проход 2 из 2 (кодирование)…"
            rc, errs = core.run_ffmpeg_progress(
                common + ["-pass", "2", "-passlogfile", passlog,
                          "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                          "-movflags", "+faststart", str(out)],
                job, duration, base=50.0, span=50.0)
            if job["status"] == "canceled":
                shutil.rmtree(work, ignore_errors=True)
                return
            if rc != 0:
                job["status"] = "error"
                job["error"] = "; ".join(errs[-2:]) or f"ffmpeg pass 2 exit code {rc}"
                return
        else:
            # target_mb == 0: без привязки к размеру файла — только разрешение
            # и звук (см. README «сжать»). Однопроходный CRF-энкод: без цели
            # по размеру двухпроходность не нужна, CRF сам держит качество.
            suffix = f" [{height}p]" if height > 0 else ""
            out = work / (src.stem.replace("source", "video") + suffix + ".mp4")
            common = (["-i", str(src), "-c:v", "libx264", "-crf", "20",
                      "-preset", "medium", "-pix_fmt", "yuv420p"] + scale_args)

            job["status"] = "processing"
            job["progress"] = 0.0
            job["stage"] = "Кодирование…"
            rc, errs = core.run_ffmpeg_progress(
                common + ["-c:a", "aac", "-b:a", f"{audio_kbps}k",
                          "-movflags", "+faststart", str(out)],
                job, duration, base=0.0, span=100.0)
            if job["status"] == "canceled":
                shutil.rmtree(work, ignore_errors=True)
                return
            if rc != 0:
                job["status"] = "error"
                job["error"] = "; ".join(errs[-2:]) or f"ffmpeg exit code {rc}"
                return

        if not out.is_file():
            job["status"] = "error"
            job["error"] = "Итоговый файл не создан."
            return

        size_mb = out.stat().st_size / (1024 * 1024)
        job["filename"] = str(out)
        job["download_name"] = out.name
        job["result_size_mb"] = round(size_mb, 2)
        job["progress"] = 100.0
        job["stage"] = f"Готово — {size_mb:.1f} МБ"
        job["status"] = "done"
        core.logger.info("Сжать: готово (%s) -> %s (%.1f МБ)",
                         job.get("title"), out.name, size_mb)
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        core.logger.error("Сжать: исключение (%s): %s", job.get("title"), e)


# --- Роуты -------------------------------------------------------------------

@bp.route("/")
def page():
    return render_template("compress.html")


@bp.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Файл не выбран"}), 400
    dep_err = core.missing_dependencies_payload("compress")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    job_id = uuid.uuid4().hex[:12]
    work = core.job_dir(job_id)

    stem, ext = os.path.splitext(f.filename)
    safe = core.safe_filename(stem) + (ext or ".mp4")
    dst = work / ("source_" + safe)
    f.save(str(dst))

    duration = core.ffprobe_duration(dst)
    _w, src_height = core.ffprobe_resolution(dst)
    size_mb = dst.stat().st_size / (1024 * 1024)

    with core.JOBS_LOCK:
        core.cleanup_old_jobs()
        core.JOBS[job_id] = core.new_job({
            "status": "ready", "title": f.filename,
            "src_path": str(dst), "duration": duration,
            "src_height": src_height,
        })
    return jsonify({"job_id": job_id, "title": f.filename,
                    "duration": duration, "size_mb": round(size_mb, 2),
                    "src_height": src_height})


@bp.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL не указан"}), 400

    # core.resolve_video_or_playlist() объединяет быструю проверку --flat-playlist
    # (без неё полный dump-single-json на ссылке-плейлисте утыкается в таймаут)
    # и получение полных метаданных одиночного видео в один проход.
    is_pl, info, err = core.resolve_video_or_playlist(url)
    if err:
        return jsonify({"error": err}), 400
    if is_pl:
        return jsonify({"error": "Ссылки на плейлисты поддерживаются только "
                                 "в разделе «скачать»."}), 400
    # err уже проверен выше — гарантированно непуст, когда info == None
    # (см. resolve_video_or_playlist); проверка оставлена defensively.
    if not info:
        return jsonify({"error": "Не удалось получить информацию о видео."}), 400

    title = info.get("title") or "video"
    duration = info.get("duration") or 0
    # Максимальная доступная высота (исходное разрешение).
    src_height = info.get("height") or 0
    if not src_height:
        src_height = max((f.get("height") or 0)
                         for f in (info.get("formats") or [{}]))
    job_id = uuid.uuid4().hex[:12]
    with core.JOBS_LOCK:
        core.cleanup_old_jobs()
        core.JOBS[job_id] = core.new_job({
            "status": "ready", "title": title, "url": url, "duration": duration,
            "src_height": src_height, "video_id": info.get("id") or "",
        })
    return jsonify({"job_id": job_id, "title": title, "duration": duration,
                    "src_height": src_height})


@bp.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    try:
        target_mb = float(data.get("target_mb"))
    except (TypeError, ValueError):
        return jsonify({"error": "Укажите целевой размер (МБ)."}), 400
    # 0 — валидное значение: сжатие без привязки к размеру файла, только по
    # разрешению и звуку (см. _compress_thread).
    if target_mb < 0:
        return jsonify({"error": "Целевой размер не может быть отрицательным."}), 400
    audio_kbps = int(data.get("audio_kbps") or 128)
    height = int(data.get("height") or 0)   # 0 = исходное разрешение
    dep_err = core.missing_dependencies_payload("compress")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Задача не найдена. "
                                     "Загрузите видео заново."}), 404
        if job["status"] in ("downloading", "processing"):
            return jsonify({"error": "Эта задача уже выполняется."}), 409
        job.update({
            "status": "pending", "progress": 0.0, "error": "", "stage": "",
            "filename": "", "download_name": "", "result_size_mb": None,
            "target_mb": target_mb, "audio_kbps": audio_kbps, "height": height,
        })

    t = threading.Thread(target=_compress_thread, args=(job_id, job),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})
