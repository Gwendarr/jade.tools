# -*- coding: utf-8 -*-
"""Инструмент: скачивание видео/звука с YouTube (и других сайтов) через yt-dlp."""

import re
import uuid
import shutil
import zipfile
import threading
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify

import core

bp = Blueprint("youtube", __name__, url_prefix="/youtube")

TOOL = {
    "id": "youtube",
    "name": "скачать",
    "desc": "видео и звук с YouTube и других сайтов через yt-dlp",
    "url": "/youtube/",
    "icon": ('<svg viewBox="0 0 24 24"><path d="M12 3v12m0 0l-4-4m4 4l4-4'
             'M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>'),
}

# --- Поддерживаемые форматы --------------------------------------------------

VIDEO_FORMATS = {
    "mp4":  {"ext": "mp4",  "label": "MP4 (H.264/AAC)",   "vcodec": "avc1", "acodec": "mp4a"},
    "webm": {"ext": "webm", "label": "WebM (VP9/Opus)",   "vcodec": "vp9",  "acodec": "opus"},
    "mkv":  {"ext": "mkv",  "label": "MKV (лучший кодек)", "vcodec": None,   "acodec": None},
}
AUDIO_FORMATS = {
    "mp3":  {"ext": "mp3",  "label": "MP3 (универсальный)"},
    "m4a":  {"ext": "m4a",  "label": "M4A / AAC (Apple)"},
    "wav":  {"ext": "wav",  "label": "WAV (без потерь)"},
    "opus": {"ext": "opus", "label": "Opus (эффективный)"},
}

_PROGRESS_RE = re.compile(
    r'\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+~?\s*(?P<size>[^\s]+)'
    r'(?:\s+at\s+(?P<speed>[^\s]+))?(?:\s+ETA\s+(?P<eta>[^\s]+))?'
)


def _summarize_info(info):
    """Превратить сырой dump-json в удобный для фронта набор данных."""
    formats_raw = info.get("formats", []) or []

    video_formats, audio_formats, best_combined = [], [], []

    for f in formats_raw:
        vcodec = (f.get("vcodec") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        height = f.get("height") or 0
        fps = f.get("fps") or 0
        abr = f.get("abr") or f.get("tbr") or 0
        ext = f.get("ext") or ""
        f_id = f.get("format_id")
        if not f_id:
            continue

        has_video = vcodec not in ("", "none")
        has_audio = acodec not in ("", "none")

        if has_video and not has_audio:
            label = f"{int(height)}p" + (f"@{int(fps)}" if fps else "")
            video_formats.append({"id": f_id, "label": label,
                                  "height": int(height), "ext": ext})
        elif has_video and has_audio:
            label = f"{int(height)}p" + (f"@{int(fps)}" if fps else "")
            best_combined.append({"id": f_id, "label": label,
                                  "height": int(height), "ext": ext})
        elif has_audio and not has_video:
            label = f"{int(abr)}kbps" if abr else ext
            audio_formats.append({"id": f_id, "label": label, "ext": ext,
                                  "abr": int(abr)})

    def _dedupe_by_key(lst, key):
        seen, out = set(), []
        for it in sorted(lst, key=lambda x: x[key], reverse=True):
            if it[key] in seen:
                continue
            seen.add(it[key])
            out.append(it)
        return out

    video_formats = _dedupe_by_key(video_formats, "height")
    best_combined = _dedupe_by_key(best_combined, "height")
    audio_formats = _dedupe_by_key(audio_formats, "abr")

    combined_heights, seen_h = [], set()
    all_video = sorted(video_formats + best_combined,
                       key=lambda x: x["height"], reverse=True)
    for v in all_video:
        h = v["height"]
        if h in seen_h or h == 0:
            continue
        seen_h.add(h)
        combined_heights.append({"label": f"{h}p", "height": h})

    return {
        "title": info.get("title") or info.get("id") or "video",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "uploader": info.get("uploader") or "",
        "video_only": video_formats,
        "combined_heights": combined_heights,
        "audio": audio_formats,
    }


# --- Плейлисты ----------------------------------------------------------------
# Общие настройки формата/качества выбираются один раз на весь плейлист (см.
# api_download_playlist) и применяются к каждому отмеченному видео как к
# отдельной задаче — так переиспользуется вся существующая система JOBS
# (прогресс/отмена/кэш) без единой команды yt-dlp на весь плейлист сразу.

# Стандартный набор высот для выбора качества плейлиста: --flat-playlist не
# даёт полного списка форматов на каждое видео (иначе это была бы та же самая
# медленная операция, от которой мы уходим), поэтому качество выбирается из
# общего набора, а не из фактически доступных для каждого ролика вариантов.
PLAYLIST_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]


def _thumbnail_url(d):
    """Лучшее превью из "thumbnails" (список) или запасной вариант по id видео."""
    thumbs = (d or {}).get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        if best.get("url"):
            return best["url"]
    vid = (d or {}).get("id")
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""


def _summarize_playlist(data):
    """Из flat-playlist dump собрать удобный для фронта список видео.

    Отдельные записи плейлиста (entries) часто НЕ содержат uploader/channel,
    если весь плейлист одного канала — тогда берём канал самого плейлиста."""
    top_uploader = data.get("uploader") or data.get("channel") or ""
    entries = []
    for e in (data.get("entries") or []):
        vid = e.get("id")
        if not vid:
            continue
        entries.append({
            "id": vid,
            "title": e.get("title") or vid,
            "duration": e.get("duration") or 0,
            "uploader": e.get("uploader") or e.get("channel") or top_uploader,
            "thumbnail": _thumbnail_url(e),
        })
    return {
        "title": data.get("title") or "Плейлист",
        "uploader": top_uploader,
        "thumbnail": _thumbnail_url(data),
        "entries": entries,
        "heights": PLAYLIST_HEIGHTS,
    }


def _parse_progress(line, job):
    m = _PROGRESS_RE.search(line)
    if not m:
        return False
    try:
        job["progress"] = float(m.group("pct"))
    except ValueError:
        pass
    if m.group("speed"):
        job["speed"] = m.group("speed")
    if m.group("eta"):
        job["eta"] = m.group("eta")
    return True


def _format_args(job_id, job):
    """Собирает аргументы yt-dlp под выбранный режим, качество и формат."""
    mode = job.get("mode", "video_audio")
    height = job.get("height")
    audio_id = job.get("audio_id")
    video_fmt_key = job.get("video_format") or "mp4"
    audio_fmt_key = job.get("audio_format") or "mp3"

    out_tmpl = str(core.DOWNLOADS_DIR / job_id / "%(title).200B [%(id)s].%(ext)s")
    args = ["-o", out_tmpl]
    embed = core.get_settings().get("embed_metadata", True)

    if mode == "audio_only":
        af = AUDIO_FORMATS.get(audio_fmt_key, AUDIO_FORMATS["mp3"])
        args += ["-x", "--audio-format", af["ext"]]
        if af["ext"] == "mp3":
            args += ["--audio-quality", "0"]
        if audio_id:
            args += ["-f", str(audio_id)]
        else:
            pref = {"mp3": "m4a", "m4a": "m4a", "wav": "m4a",
                    "opus": "opus"}[af["ext"]]
            args += ["-f", f"bestaudio[ext={pref}]/bestaudio/best"]
        # Обложка+метаданные: полноценно для mp3/m4a, best-effort для opus
        # (контейнер поддерживает встраивание хуже, но yt-dlp сам сделает что
        # сможет). WAV embedding не поддерживает вовсе — не пытаемся.
        if embed and af["ext"] != "wav":
            args += ["--embed-metadata", "--embed-thumbnail"]
        return args

    vf = VIDEO_FORMATS.get(video_fmt_key, VIDEO_FORMATS["mp4"])

    if vf["vcodec"]:
        base_v = f"bestvideo[vcodec^={vf['vcodec']}]"
        codec_pref = f"[vcodec^={vf['vcodec']}]"
    else:
        base_v = "bestvideo"
        codec_pref = ""
    if height:
        video_sel = f"{base_v}[height<={height}]/bestvideo[height<={height}]/bestvideo"
    else:
        video_sel = f"{base_v}/bestvideo"
    video_sel = f"({video_sel})"

    # Для видео встраиваем только метаданные (название, канал) — не превью:
    # обложки в видео плохо поддерживаются плеерами (и совсем не поддерживаются
    # WebM), а выигрыш от них несопоставим с усилиями на реализацию.
    embed_video = embed and vf["ext"] in ("mp4", "mkv")

    if mode == "video_only":
        args += ["-f", video_sel, "--remux-video", vf["ext"]]
        if embed_video:
            args += ["--embed-metadata"]
        return args

    if vf["acodec"]:
        base_a = f"bestaudio[acodec^={vf['acodec']}]"
        fallback_a = "bestaudio"
    else:
        base_a = "bestaudio"
        fallback_a = "bestaudio"
    fmt = (f"{video_sel}+({base_a}/{fallback_a})/"
           f"best[height<={height}]{codec_pref}/best" if height else
           f"{video_sel}+({base_a}/{fallback_a})/best")
    args += ["-f", fmt, "--remux-video", vf["ext"]]
    if embed_video:
        args += ["--embed-metadata"]
    return args


def _download_thread(job_id, job, url):
    work_dir = core.job_dir(job_id)
    try:
        # Кэш: тот же ролик в той же спецификации уже качался — отдаём его.
        vid = job.get("video_id") or ""
        ckey = (f"{vid}|yt|{job.get('mode')}|{job.get('height')}|"
                f"{job.get('audio_id')}|{job.get('video_format')}|"
                f"{job.get('audio_format')}") if vid else ""
        cached = core.cache_get(ckey) if ckey else None
        if cached:
            core.logger.info("Скачать: из кэша (%s) -> %s",
                             job.get("title") or url, Path(cached).name)
            with core.JOBS_LOCK:
                job["filename"] = cached
                job["download_name"] = Path(cached).name
                job["progress"] = 100.0
                job["status"] = "done"
            return

        args = _format_args(job_id, job)
        core.logger.info("Скачать: начато (%s) mode=%s height=%s",
                         job.get("title") or url, job.get("mode"), job.get("height"))

        with core.JOBS_LOCK:
            job["status"] = "downloading"
            job["error"] = ""
            job["progress"] = 0.0

        rc, err_tail = core.run_ytdlp_download(
            lambda cookies: core.ytdlp_cmd(*args, "--newline", "--progress",
                                           url, cookies=cookies),
            job, work_dir, _parse_progress,
        )
        if rc is None:
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        if rc != 0:
            job["status"] = "error"
            job["error"] = (core.friendly_ytdlp_error("\n".join(err_tail))
                            if err_tail else f"yt-dlp exit code {rc}")
            core.logger.warning("Скачать: ошибка (%s): %s",
                                job.get("title") or url, job["error"])
            return

        files = [p for p in work_dir.rglob("*") if p.is_file()]
        if not files:
            job["status"] = "error"
            job["error"] = "Файл не найден после скачивания."
            core.logger.warning("Скачать: файл не найден после скачивания (%s)",
                                job.get("title") or url)
            return
        result = max(files, key=lambda p: (p.stat().st_mtime, p.stat().st_size))
        if ckey:
            core.cache_put(ckey, str(result))   # для повторного использования
        job["filename"] = str(result)
        job["download_name"] = result.name
        job["progress"] = 100.0
        job["status"] = "done"
        core.logger.info("Скачать: готово (%s) -> %s",
                         job.get("title") or url, result.name)
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        core.logger.error("Скачать: исключение (%s): %s", url, e)


def _download_playlist_thread(parent_id, delivery, concurrency=None):
    """Оркестратор плейлиста: качает отмеченные видео — параллельно, не более
    `concurrency` одновременно (None = без ограничения), по одной задаче на
    видео (переиспользуя _download_thread целиком — прогресс, отмена и кэш
    работают в точности как для одиночного видео). Лимит фиксируется на
    момент запуска этой задачи и не меняется по ходу её выполнения, даже если
    настройка изменится. В конце — либо просто завершает (потоковая доставка:
    каждое видео браузер уже забрал сам по мере готовности), либо упаковывает
    успешно скачанные файлы в один .zip (доставка архивом)."""
    with core.JOBS_LOCK:
        parent = core.JOBS.get(parent_id)
        sub_ids = list(parent.get("sub_jobs") or []) if parent else []
    if not parent:
        return

    done_files = []   # [(job_id, path, download_name), ...] — только успешные
    state = {"done_count": 0, "canceled": False}

    def run_one(child_id, child):
        url = f"https://www.youtube.com/watch?v={child.get('video_id')}"
        try:
            _download_thread(child_id, child, url)
        except Exception as e:
            child["status"] = "error"
            child["error"] = f"{type(e).__name__}: {e}"

        with core.JOBS_LOCK:
            state["done_count"] += 1
            if child.get("status") == "done" and child.get("filename"):
                done_files.append((child_id, child["filename"], child.get("download_name") or ""))
            parent["done_count"] = state["done_count"]
            parent["progress"] = round(state["done_count"] / max(1, parent["total_count"]) * 100, 1)
            parent["stage"] = f"{state['done_count']} из {parent['total_count']} скачано"
            if parent.get("status") == "canceled":
                state["canceled"] = True

    sem = threading.Semaphore(concurrency) if concurrency else None
    threads = []
    for child_id in sub_ids:
        with core.JOBS_LOCK:
            if parent.get("status") == "canceled":
                state["canceled"] = True
        if state["canceled"]:
            break
        child = core.JOBS.get(child_id)
        if not child:
            continue

        if sem:
            sem.acquire()

        def worker(child_id=child_id, child=child):
            try:
                run_one(child_id, child)
            finally:
                if sem:
                    sem.release()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    if state["canceled"]:
        core.logger.info("Плейлист: отменено пользователем (%s)", parent.get("title"))
        return

    if delivery == "stream":
        # Каждый файл браузер уже забирал по мере готовности (см. фронт) —
        # здесь просто фиксируем итог по всему плейлисту.
        with core.JOBS_LOCK:
            parent["status"] = "done"
            parent["progress"] = 100.0
            parent["stage"] = f"Готово — {len(done_files)} из {parent['total_count']}"
        core.logger.info("Плейлист: потоковая загрузка завершена (%s), успешно %s/%s",
                         parent.get("title"), len(done_files), parent["total_count"])
        return

    # Доставка архивом: упаковываем успешно скачанные файлы в один .zip.
    if not done_files:
        with core.JOBS_LOCK:
            parent["status"] = "error"
            parent["error"] = "Не удалось скачать ни одного видео из выбранных."
        return

    with core.JOBS_LOCK:
        parent["status"] = "processing"
        parent["stage"] = "Упаковка в архив…"

    work = core.job_dir(parent_id)
    playlist_title = core.safe_filename(parent.get("title") or "playlist")
    archive_name = f"{playlist_title} [{len(done_files)} ep].zip"
    archive_path = work / archive_name
    try:
        used_names = set()
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
            for _cid, path, dl_name in done_files:
                name = dl_name or Path(path).name
                stem, ext = Path(name).stem, Path(name).suffix
                final_name, i = name, 1
                while final_name in used_names:
                    final_name = f"{stem} ({i}){ext}"
                    i += 1
                used_names.add(final_name)
                zf.write(path, arcname=final_name)

        with core.JOBS_LOCK:
            parent["filename"] = str(archive_path)
            parent["download_name"] = archive_name
            parent["result_size_mb"] = round(archive_path.stat().st_size / (1024 * 1024), 2)
            parent["progress"] = 100.0
            parent["stage"] = f"Готово — архив из {len(done_files)} видео"
            parent["status"] = "done"
        core.logger.info("Плейлист: архив готов (%s) -> %s",
                         parent.get("title"), archive_name)
    except Exception as e:
        with core.JOBS_LOCK:
            parent["status"] = "error"
            parent["error"] = f"Не удалось собрать архив: {e}"
        core.logger.error("Плейлист: ошибка упаковки архива (%s): %s",
                          parent.get("title"), e)


# --- Роуты -------------------------------------------------------------------

@bp.route("/")
def page():
    return render_template("youtube.html")


@bp.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL не указан"}), 400
    if not core.is_supported_url(url):
        return jsonify({"error": "Поддерживаются только ссылки http:// или https://."}), 400
    dep_err = core.missing_dependencies_payload("youtube")
    if dep_err:
        return jsonify(dep_err), 400

    # core.resolve_video_or_playlist() объединяет быструю проверку --flat-playlist
    # (определяет плейлист за секунды, без попытки вытянуть полные форматы
    # каждого видео — это и вызывало таймаут 120с на ссылках-плейлистах) и
    # получение полных метаданных одиночного видео в один проход — второй
    # вызов делается, только если flat-ответ не принёс formats сам.
    is_pl, result, err = core.resolve_video_or_playlist(url)
    if err:
        return jsonify({"error": err}), 400

    if is_pl:
        pl = _summarize_playlist(result)
        if not pl["entries"]:
            return jsonify({"error": "Плейлист пуст или все видео в нём недоступны."}), 400
        job_id = uuid.uuid4().hex[:12]
        with core.JOBS_LOCK:
            core.cleanup_old_jobs()
            core.JOBS[job_id] = core.new_job({
                "status": "ready", "title": pl["title"],
                "is_playlist": True, "playlist_entries": pl["entries"],
            })
        return jsonify({"job_id": job_id, "is_playlist": True, "playlist": pl})

    # err уже проверен выше (return при err) — resolve_video_or_playlist()
    # гарантирует err непустым всегда, когда result == None, так что сюда с
    # info==None в норме попасть нельзя; проверка оставлена defensively.
    info = result
    if not info:
        return jsonify({"error": "Не удалось получить информацию. "
                                 "Проверьте ссылку и доступ к интернету."}), 400

    summary = _summarize_info(info)
    job_id = uuid.uuid4().hex[:12]
    with core.JOBS_LOCK:
        core.cleanup_old_jobs()
        core.JOBS[job_id] = core.new_job({
            "status": "ready", "title": summary["title"], "info": summary,
            "video_id": info.get("id") or "",
        })
    return jsonify({"job_id": job_id, "is_playlist": False, "info": summary})


@bp.route("/api/formats")
def api_formats():
    return jsonify({
        "video": [{"key": k, "label": v["label"]} for k, v in VIDEO_FORMATS.items()],
        "audio": [{"key": k, "label": v["label"]} for k, v in AUDIO_FORMATS.items()],
    })


@bp.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    job_id = (data.get("job_id") or "").strip()
    mode = data.get("mode") or "video_audio"
    height = core.parse_int(data.get("height"), 0)
    audio_id = data.get("audio_id") or ""
    video_format = (data.get("video_format") or "mp4").strip()
    audio_format = (data.get("audio_format") or "mp3").strip()

    if not url or not job_id:
        return jsonify({"error": "Неверный запрос (нужны url и job_id)"}), 400
    if not core.is_supported_url(url):
        return jsonify({"error": "Поддерживаются только ссылки http:// или https://."}), 400
    if mode not in ("video_audio", "video_only", "audio_only"):
        return jsonify({"error": "Неизвестный режим"}), 400
    if video_format not in VIDEO_FORMATS:
        return jsonify({"error": "Неподдерживаемый формат видео"}), 400
    if audio_format not in AUDIO_FORMATS:
        return jsonify({"error": "Неподдерживаемый формат звука"}), 400
    dep_err = core.missing_dependencies_payload("youtube")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Задача не найдена. "
                                     "Получите информацию заново."}), 404
        if core.is_job_active(job):
            return jsonify({"error": "Эта задача уже скачивается"}), 409
        job.update({
            "status": "pending", "progress": 0.0, "speed": "", "eta": "",
            "error": "", "filename": "", "download_name": "",
            "mode": mode, "height": height, "audio_id": audio_id,
            "video_format": video_format, "audio_format": audio_format,
        })

    t = threading.Thread(target=_download_thread, args=(job_id, job, url),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "ok": True})


@bp.route("/api/download_playlist", methods=["POST"])
def api_download_playlist():
    """Запускает скачивание отмеченных видео плейлиста: настройки формата
    общие на весь плейлист, но каждое видео — своя задача в core.JOBS (со
    своим прогрессом/отменой/кэшем), скачиваются последовательно одним
    фоновым потоком-оркестратором."""
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    mode = data.get("mode") or "video_audio"
    height = core.parse_int(data.get("height"), 0)
    audio_id = data.get("audio_id") or ""
    video_format = (data.get("video_format") or "mp4").strip()
    audio_format = (data.get("audio_format") or "mp3").strip()
    selected_ids = data.get("selected_ids") or []
    delivery = (data.get("delivery") or "zip").strip()

    if mode not in ("video_audio", "video_only", "audio_only"):
        return jsonify({"error": "Неизвестный режим"}), 400
    if video_format not in VIDEO_FORMATS:
        return jsonify({"error": "Неподдерживаемый формат видео"}), 400
    if audio_format not in AUDIO_FORMATS:
        return jsonify({"error": "Неподдерживаемый формат звука"}), 400
    if delivery not in ("zip", "stream"):
        return jsonify({"error": "Неизвестный способ доставки"}), 400
    if not isinstance(selected_ids, list) or not selected_ids:
        return jsonify({"error": "Выберите хотя бы одно видео."}), 400
    dep_err = core.missing_dependencies_payload("youtube")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("is_playlist"):
            return jsonify({"error": "Плейлист не найден. "
                                     "Получите информацию заново."}), 404
        if job["status"] in ("downloading", "processing"):
            return jsonify({"error": "Эта задача уже выполняется."}), 409

        entries_by_id = {e["id"]: e for e in job.get("playlist_entries") or []}
        selected = [entries_by_id[i] for i in selected_ids if i in entries_by_id]
        if not selected:
            return jsonify({"error": "Выбранные видео не найдены в плейлисте."}), 400

        sub_jobs = []
        for e in selected:
            child_id = uuid.uuid4().hex[:12]
            core.JOBS[child_id] = core.new_job({
                "status": "pending", "title": e["title"], "video_id": e["id"],
                "parent_job_id": job_id,
                "mode": mode, "height": height, "audio_id": audio_id,
                "video_format": video_format, "audio_format": audio_format,
            })
            sub_jobs.append({
                "job_id": child_id, "video_id": e["id"], "title": e["title"],
                "duration": e.get("duration") or 0,
                "uploader": e.get("uploader") or "",
                "thumbnail": e.get("thumbnail") or "",
            })

        job.update({
            "status": "downloading", "progress": 0.0, "error": "", "stage": "",
            "filename": "", "download_name": "",
            "sub_jobs": [sj["job_id"] for sj in sub_jobs],
            "delivery": delivery, "done_count": 0, "total_count": len(sub_jobs),
        })

    # Лимит параллельных загрузок фиксируется на момент ЗАПУСКА (настройка
    # могла бы измениться, пока задача уже выполняется — на неё это не
    # повлияет, как и требуется). "unlimited" -> None (без ограничения).
    limit_setting = str(core.get_settings().get("max_concurrent_downloads", "3"))
    concurrency = None if limit_setting == "unlimited" else (int(limit_setting) if limit_setting.isdigit() else 3)

    t = threading.Thread(target=_download_playlist_thread,
                         args=(job_id, delivery, concurrency), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "sub_jobs": sub_jobs})


@bp.route("/api/playlist_status/<job_id>")
def api_playlist_status(job_id):
    """Агрегированный статус плейлиста + статус каждого отдельного видео —
    одним запросом (не N запросов на N видео)."""
    with core.JOBS_LOCK:
        parent = core.JOBS.get(job_id)
        if not parent or not parent.get("is_playlist"):
            return jsonify({"error": "not found"}), 404
        children = []
        for cid in parent.get("sub_jobs") or []:
            c = core.JOBS.get(cid)
            if not c:
                children.append({"job_id": cid, "status": "error",
                                 "progress": 0, "error": "задача утеряна"})
                continue
            children.append({
                "job_id": cid, "status": c["status"],
                "progress": round(c.get("progress", 0), 1),
                "error": c.get("error", ""),
                "download_name": c.get("download_name", ""),
            })
        return jsonify({
            "status": parent["status"],
            "progress": round(parent.get("progress", 0), 1),
            "stage": parent.get("stage", ""),
            "error": parent.get("error", ""),
            "done_count": parent.get("done_count", 0),
            "total_count": parent.get("total_count", 0),
            "download_name": parent.get("download_name", ""),
            "children": children,
        })
