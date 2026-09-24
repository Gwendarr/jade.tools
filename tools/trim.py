# -*- coding: utf-8 -*-
"""Инструмент «Нарезка»: обрезка аудио и видео по таймингам.

Источник — загруженный файл (аудио или видео) или ссылка (YouTube и другие
сайты, которые поддерживает yt-dlp — тогда ролик сначала скачивается во
временную папку в выбранном разрешении). Дальше
строится огибающая волны и, для видео, полоса кадров (видеоряд); пользователь
выбирает фрагмент (поля таймингов + перетаскиваемые ручки, превью на плеере) и
сохраняет его. Для видео-источника на выходе — видео-фрагмент (mp4) или только
звук (mp3/m4a/wav/opus); для аудио — соответствующий аудиоформат.

На краях реза накладывается микро-fade (убирает щелчки). Из одного источника
можно нарезать несколько фрагментов — оригинал живёт до авто-очистки.
"""

import os
import re
import array
import uuid
import shutil
import threading
import subprocess
import urllib.request
from pathlib import Path

from flask import (Blueprint, render_template, request, jsonify, send_file,
                   abort)

import core

bp = Blueprint("trim", __name__, url_prefix="/trim")

TOOL = {
    "id": "trim",
    "name": "нарезать",
    "desc": "вырезать фрагмент из аудио или видео",
    "url": "/trim/",
    "icon": ('<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/>'
             '<circle cx="6" cy="18" r="3"/>'
             '<path d="M20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12"/></svg>'),
}

# Форматы вывода. Видео-источник можно сохранить как видео (mp4) или как звук.
AUDIO_FORMATS = {
    "mp3":  {"ext": "mp3",  "label": "MP3"},
    "m4a":  {"ext": "m4a",  "label": "M4A / AAC"},
    "wav":  {"ext": "wav",  "label": "WAV (без потерь)"},
    "opus": {"ext": "opus", "label": "Opus"},
}
VIDEO_FORMATS = {
    "mp4":  {"ext": "mp4",  "label": "MP4 (видео)"},
}

_PEAK_BUCKETS = 1600
_PEAK_SR = 8000
# Полоса кадров: кадров много (для зума), фронт рисует их в натуральных
# пропорциях, выбирая ближайший по времени — поэтому кадры не сжимаются.
_FILMSTRIP_FRAMES = 120
_FILMSTRIP_HEIGHT = 90       # высота кадра полосы, px

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


# --- Анализ источника --------------------------------------------------------

def _generate_peaks(path, audio_index=0, buckets=_PEAK_BUCKETS):
    """Огибающая громкости выбранной аудиодорожки: значения 0..1 (пусто без звука)."""
    try:
        proc = subprocess.run(
            [core.FFMPEG_BIN, "-v", "error", "-i", str(path),
             "-map", f"0:a:{audio_index}?",
             "-ac", "1", "-ar", str(_PEAK_SR), "-f", "s16le", "-"],
            capture_output=True, timeout=300, creationflags=core._NO_WINDOW,
        )
    except Exception:
        return []
    raw = proc.stdout
    if not raw:
        return []
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    n = len(samples)
    if n == 0:
        return []
    bucket = max(1, n // buckets)
    peaks = []
    for i in range(0, n, bucket):
        chunk = samples[i:i + bucket]
        if not chunk:
            break
        peaks.append(max(max(chunk), -min(chunk)))
    mx = max(peaks) or 1
    return [round(p / mx, 3) for p in peaks]


def _has_real_video(path):
    """True, если в файле есть НАСТОЯЩИЙ видеопоток (а не обложка-картинка)."""
    try:
        proc = subprocess.run(
            [core.FFPROBE_BIN, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream_disposition=attached_pic",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=core._NO_WINDOW,
        )
    except Exception:
        return False
    # по строке на видеопоток: "1" = обложка, "0" = реальное видео
    return any(line.strip() == "0" for line in proc.stdout.splitlines())


def _ffprobe_audio_tracks(path):
    """Список аудиодорожек: [{index, label}]. index — порядковый (0-based) среди
    аудиопотоков (для ffmpeg -map 0:a:index), label — название/язык, если есть."""
    try:
        import json as _json
        proc = subprocess.run(
            [core.FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index:stream_tags=language,title",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=core._NO_WINDOW,
        )
        streams = (_json.loads(proc.stdout or "{}")).get("streams", [])
    except Exception:
        return []
    out = []
    for i, s in enumerate(streams):
        tags = s.get("tags") or {}
        label = (tags.get("title") or "").strip()
        if not label:
            lang = (tags.get("language") or "").strip()
            if lang and lang.lower() != "und":   # "und" = язык не указан
                label = lang
        out.append({"index": i, "label": label})
    return out


def _ffprobe_fps(path):
    """Частота кадров видео (float). 0.0, если определить не удалось."""
    try:
        proc = subprocess.run(
            [core.FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=core._NO_WINDOW,
        )
        val = proc.stdout.strip()
        if "/" in val:
            a, b = val.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(val) if val else 0.0
    except Exception:
        return 0.0


def _make_filmstrip(src, out, a, b, n, height=_FILMSTRIP_HEIGHT):
    """Полоса из n кадров для участка [a, b] видео. Путь или None."""
    dur = b - a
    if dur <= 0 or n < 1:
        return None
    fps = n / dur
    vf = f"fps={fps:.6f},scale=-1:{height},tile={n}x1"
    try:
        subprocess.run(
            [core.FFMPEG_BIN, "-v", "error", "-y", "-ss", f"{a:.3f}", "-i", str(src),
             "-t", f"{dur:.3f}", "-vf", vf, "-frames:v", "1", str(out)],
            capture_output=True, timeout=300, creationflags=core._NO_WINDOW,
        )
    except Exception:
        return None
    return str(out) if Path(out).is_file() else None


def _analyze_source(job, src):
    """Заполнить job данными источника: длительность, волна, видео, видеоряд."""
    src = Path(src)
    duration = core.ffprobe_duration(src)
    has_video = _has_real_video(src)
    w, h = core.ffprobe_resolution(src) if has_video else (0, 0)
    fps = _ffprobe_fps(src) if has_video else 0.0
    peaks = _generate_peaks(src)
    audio_tracks = _ffprobe_audio_tracks(src)
    # Полосу кадров генерим не заранее, а по запросу под видимый участок (зум).

    job["src_path"] = str(src)
    job["duration"] = duration
    job["has_video"] = has_video
    job["audio_tracks"] = audio_tracks
    job["info"] = {
        "title": job.get("title") or src.stem,
        "duration": duration,
        "peaks": peaks,
        "has_video": has_video,
        "width": w,
        "height": h,
        "fps": round(fps, 3),
        "filmstrip": has_video,   # видеоряд доступен (строится по запросу)
        "audio_tracks": audio_tracks,
    }


# --- YouTube: метаданные и скачивание во временную папку ----------------------

def _summarize_heights(info):
    """Список доступных высот видео (по убыванию) из метаданных yt-dlp."""
    heights = set()
    for f in info.get("formats", []) or []:
        if (f.get("vcodec") or "none") != "none" and f.get("height"):
            heights.add(int(f["height"]))
    return sorted(heights, reverse=True)


def _prepare_thread(job_id, job, url, height):
    """Скачать ролик во временную папку и проанализировать (волна + видеоряд)."""
    work = core.job_dir(job_id)
    try:
        # Кэш: тот же ролик в той же спецификации уже качался — не качаем заново.
        vid = job.get("video_id") or ""
        spec = ("v" + str(height)) if job.get("has_video") else "a"
        ckey = f"{vid}|trim|{spec}" if vid else ""
        cached = core.cache_get(ckey) if ckey else None
        if cached:
            core.logger.info("Нарезать: источник из кэша -> %s", Path(cached).name)
            job["status"] = "processing"
            job["stage"] = "Из кэша · анализ (волна и кадры)…"
            job["progress"] = 100.0
            if not job.get("title"):
                job["title"] = Path(cached).stem
            _analyze_source(job, cached)
            job["stage"] = ""
            job["status"] = "ready"
            return

        job["status"] = "downloading"
        job["stage"] = "Скачивание во временную папку…"
        job["progress"] = 0.0
        core.logger.info("Нарезать: скачивание начато (%s), height=%s", url, height)

        out_tmpl = str(work / "%(title).150B.%(ext)s")
        if job.get("has_video"):
            sel = (f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
                   if height else "bv*+ba/b")
            build_cmd = lambda cookies: core.ytdlp_cmd(
                "-f", sel, "--merge-output-format", "mp4",
                "-o", out_tmpl, "--newline", "--progress", url, cookies=cookies)
        else:
            build_cmd = lambda cookies: core.ytdlp_cmd(
                "-f", "bestaudio/best",
                "-o", out_tmpl, "--newline", "--progress", url, cookies=cookies)

        rc, err_tail = core.run_ytdlp_download(
            build_cmd, job, work, _parse_dl_progress,
            err_needle=("error", "ffmpeg"), tail_len=300,
        )
        if rc is None:
            shutil.rmtree(work, ignore_errors=True)
            return
        if rc != 0:
            job["status"] = "error"
            job["error"] = (core.friendly_ytdlp_error("\n".join(err_tail))
                            if err_tail else f"yt-dlp exit code {rc}")
            return

        files = [p for p in work.glob("*") if p.is_file()
                 and p.suffix.lower() != ".jpg"]   # .jpg — это полосы кадров
        if not files:
            job["status"] = "error"
            job["error"] = "Файл не найден после скачивания."
            return
        src = max(files, key=lambda p: p.stat().st_mtime)
        if ckey:
            core.cache_put(ckey, src)     # сохраняем для повторного использования

        job["status"] = "processing"
        job["stage"] = "Анализ (волна и кадры)…"
        if not job.get("title"):
            job["title"] = src.stem
        _analyze_source(job, src)

        job["progress"] = 100.0
        job["stage"] = ""
        job["status"] = "ready"
        core.logger.info("Нарезать: источник готов (%s)", job.get("title"))
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        core.logger.error("Нарезать: исключение при подготовке (%s): %s", url, e)


def _analyze_upload_thread(job_id, job, dst):
    """Анализ загруженного файла в фоне (волна + видеоряд могут быть долгими)."""
    try:
        job["status"] = "processing"
        job["stage"] = "Анализ (волна и кадры)…"
        job["progress"] = 0.0
        _analyze_source(job, dst)
        if not job["info"]["peaks"] and not job["info"]["has_video"]:
            job["status"] = "error"
            job["error"] = "Не удалось прочитать файл (проверьте формат и ffmpeg)."
            return
        job["progress"] = 100.0
        job["stage"] = ""
        job["status"] = "ready"
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


# --- Обрезка -----------------------------------------------------------------

def _audio_codec_args(out_format):
    if out_format == "m4a":
        return ["-c:a", "aac", "-b:a", "192k"]
    if out_format == "wav":
        return ["-c:a", "pcm_s16le"]
    if out_format == "opus":
        return ["-c:a", "libopus", "-b:a", "160k"]
    return ["-c:a", "libmp3lame", "-q:a", "2"]   # mp3 (дефолт)


def _tag(seconds):
    """Метка времени для имени файла (двоеточие в именах Windows запрещено)."""
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return (f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s")


def _fetch_cover(url, dst):
    """Скачать обложку и привести к JPEG (Pillow — уже зависимость проекта)
    для надёжного встраивания в аудио любым источником (webp/png/...).
    True при успехе, False — best-effort, не должно ронять экспорт."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jade.tools"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.save(str(dst), "JPEG", quality=90)
        return True
    except Exception as e:
        core.logger.warning("Нарезать: не удалось подготовить обложку: %s", e)
        return False


def _trim_thread(job_id, job):
    work = core.job_dir(job_id)
    try:
        src = Path(job["src_path"])
        if not src.is_file():
            job["status"] = "error"
            job["error"] = "Исходный файл недоступен."
            return

        start = float(job["start"])
        end = float(job["end"])
        dur = end - start
        mode = job["out_mode"]            # "video" | "audio"
        out_format = job["out_format"]
        aidx = int(job.get("audio_index") or 0)
        base = core.safe_filename(src.stem)

        job["status"] = "processing"
        job["stage"] = "Нарезка…"
        job["progress"] = 0.0
        core.logger.info("Нарезать: начата обрезка (%s) %.1f-%.1fс mode=%s compress=%s",
                         job.get("title"), start, end, mode, bool(job.get("compress")))

        # Микро-fade аудио на краях — убирает щелчки.
        fade = 0.012
        afade = (f"afade=t=in:st=0:d={fade},afade=t=out:st={dur - fade:.3f}:d={fade}"
                 if dur > 4 * fade else None)

        do_compress = mode == "video" and job.get("compress")
        ext = "mp4" if mode == "video" else AUDIO_FORMATS.get(
            out_format, AUDIO_FORMATS["mp3"])["ext"]
        suffix = f" {job['target_mb']:g}MB" if do_compress else ""
        out = work / f"{base} [{_tag(start)}-{_tag(end)}]{suffix}.{ext}"
        seek = ["-ss", f"{start:.3f}", "-i", str(src)]

        # Метаданные (название, канал) — только для результатов скачивания по
        # ссылке (job["url"]), не для локально загруженных файлов (там нет
        # осмысленных title/uploader). Обложка — только в аудио, встраивается
        # ниже отдельным входом ffmpeg (не для видео — см. _format_args в
        # tools/youtube.py, та же логика и то же обоснование).
        embed = core.get_settings().get("embed_metadata", True) and bool(job.get("url"))
        meta_args = []
        if embed:
            if job.get("title"):
                meta_args += ["-metadata", f"title={job['title']}"]
            if job.get("uploader"):
                meta_args += ["-metadata", f"artist={job['uploader']}"]

        if do_compress:
            # Нарезка + сжатие под целевой размер: двухпроходный x264.
            target_mb = float(job["target_mb"])
            audio_kbps = int(job.get("audio_kbps") or 128)
            c_height = int(job.get("c_height") or 0)
            total_kbps = (target_mb * 8192) / dur * 0.97
            video_kbps = total_kbps - audio_kbps
            if video_kbps < 50:
                job["status"] = "error"
                job["error"] = ("Целевой размер слишком мал для длины фрагмента. "
                                "Увеличьте размер или уменьшите битрейт звука.")
                return
            passlog = str(work / "ffpass")
            common = seek + ["-t", f"{dur:.3f}", "-map", "0:v:0", "-map", f"0:a:{aidx}?",
                             "-c:v", "libx264", "-b:v", f"{int(video_kbps)}k",
                             "-preset", "fast", "-pix_fmt", "yuv420p"]
            if c_height > 0:
                common += ["-vf", f"scale=-2:trunc(min(ih\\,{c_height})/2)*2"]

            job["stage"] = "Сжатие — проход 1 из 2…"
            rc, errs = core.run_ffmpeg_progress(
                common + ["-pass", "1", "-passlogfile", passlog, "-an",
                          "-f", "null", os.devnull], job, dur, base=0.0, span=50.0)
            if job["status"] == "canceled":
                return
            if rc != 0:
                job["status"] = "error"
                job["error"] = "; ".join(errs[-2:]) or f"ffmpeg pass 1 exit code {rc}"
                return

            job["stage"] = "Сжатие — проход 2 из 2…"
            p2 = common + ["-pass", "2", "-passlogfile", passlog,
                           "-c:a", "aac", "-b:a", f"{audio_kbps}k"]
            if afade:
                p2 += ["-af", afade]
            p2 += meta_args + ["-movflags", "+faststart", str(out)]
            rc, errs = core.run_ffmpeg_progress(p2, job, dur, base=50.0, span=50.0)
        elif mode == "video":
            args = seek + ["-t", f"{dur:.3f}", "-map", "0:v:0", "-map", f"0:a:{aidx}?",
                           "-c:v", "libx264", "-preset", "veryfast",
                           "-crf", "20", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "192k"]
            if afade:
                args += ["-af", afade]
            args += meta_args + ["-movflags", "+faststart", str(out)]
            rc, errs = core.run_ffmpeg_progress(args, job, dur)
        else:
            # Аудио: обложка + метаданные, best-effort. WAV не поддерживает
            # встраивание вовсе (ни то, ни другое — формат ограничен) — не
            # пытаемся; Opus — как получится (тот же контейнер, что и в
            # tools/youtube.py).
            audio_meta_args = meta_args if out_format != "wav" else []
            cover = None
            if embed and out_format != "wav" and job.get("thumbnail"):
                cand = work / "cover.jpg"
                if _fetch_cover(job["thumbnail"], cand):
                    cover = cand

            args = list(seek)
            if cover:
                args += ["-i", str(cover)]
            args += ["-t", f"{dur:.3f}", "-map", f"0:a:{aidx}?"]
            if cover:
                args += ["-map", "1:v", "-c:v:1", "copy", "-disposition:v:1", "attached_pic"]
            args += _audio_codec_args(out_format)
            args += audio_meta_args
            if afade:
                args += ["-af", afade]
            args += [str(out)]
            rc, errs = core.run_ffmpeg_progress(args, job, dur)
            if rc != 0 and cover and job["status"] != "canceled":
                # Обложка — best-effort: если из-за неё сорвался весь экспорт,
                # повторяем без неё, а не проваливаем нарезку целиком.
                core.logger.warning("Нарезать: экспорт с обложкой не удался, "
                                    "повтор без неё (%s)", job.get("title"))
                args = list(seek) + ["-t", f"{dur:.3f}", "-map", f"0:a:{aidx}?"]
                args += _audio_codec_args(out_format)
                args += audio_meta_args
                if afade:
                    args += ["-af", afade]
                args += [str(out)]
                rc, errs = core.run_ffmpeg_progress(args, job, dur)

        if job["status"] == "canceled":
            return
        if rc != 0 or not out.is_file():
            job["status"] = "error"
            job["error"] = "; ".join(errs[-2:]) or f"ffmpeg exit code {rc}"
            return

        size_mb = out.stat().st_size / (1024 * 1024)
        job["filename"] = str(out)
        job["download_name"] = out.name
        job["result_size_mb"] = round(size_mb, 2)
        job["progress"] = 100.0
        job["stage"] = f"Готово — {size_mb:.1f} МБ"
        job["status"] = "done"
        core.logger.info("Нарезать: готово (%s) -> %s (%.1f МБ)",
                         job.get("title"), out.name, size_mb)
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        core.logger.error("Нарезать: исключение при обрезке (%s): %s", job.get("title"), e)


# --- Роуты -------------------------------------------------------------------

@bp.route("/")
def page():
    return render_template("trim.html")


@bp.route("/api/formats")
def api_formats():
    return jsonify({
        "audio": [{"key": k, "label": v["label"]} for k, v in AUDIO_FORMATS.items()],
        "video": [{"key": k, "label": v["label"]} for k, v in VIDEO_FORMATS.items()],
    })


@bp.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Файл не выбран"}), 400
    dep_err = core.missing_dependencies_payload("trim")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    job_id = uuid.uuid4().hex[:12]
    work = core.job_dir(job_id)
    stem, ext = os.path.splitext(f.filename)
    safe = core.safe_filename(stem) + (ext or ".mp4")
    dst = work / safe
    f.save(str(dst))

    with core.JOBS_LOCK:
        core.cleanup_old_jobs()
        job = core.new_job({"status": "pending", "title": f.filename})
        core.JOBS[job_id] = job

    t = threading.Thread(target=_analyze_upload_thread,
                         args=(job_id, job, dst), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@bp.route("/api/info", methods=["POST"])
def api_info():
    """Метаданные YouTube-ролика (без скачивания): тип, длительность, высоты."""
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
        return jsonify({"error": "Не удалось получить информацию."}), 400

    heights = _summarize_heights(info)
    job_id = uuid.uuid4().hex[:12]
    with core.JOBS_LOCK:
        core.cleanup_old_jobs()
        job = core.new_job({
            "status": "ready", "title": info.get("title") or "media",
            "url": url, "duration": info.get("duration") or 0,
            "has_video": bool(heights), "video_id": info.get("id") or "",
            "uploader": info.get("uploader") or "",
            "thumbnail": info.get("thumbnail") or "",
        })
        core.JOBS[job_id] = job
    return jsonify({
        "job_id": job_id,
        "title": job["title"],
        "duration": job["duration"],
        "has_video": bool(heights),
        "heights": heights,
    })


@bp.route("/api/prepare", methods=["POST"])
def api_prepare():
    """Скачать YouTube-ролик во временную папку и проанализировать."""
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    height = int(data.get("height") or 0)
    dep_err = core.missing_dependencies_payload("trim")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("url"):
            return jsonify({"error": "Сначала получите информацию о ролике."}), 404
        if job["status"] in ("downloading", "processing"):
            return jsonify({"error": "Уже выполняется."}), 409
        url = job["url"]
        job.update({"status": "pending", "progress": 0.0, "error": ""})

    t = threading.Thread(target=_prepare_thread,
                         args=(job_id, job, url, height), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/data/<job_id>")
def api_data(job_id):
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("info"):
            return jsonify({"error": "Данные не готовы"}), 404
        return jsonify({"job_id": job_id, **job["info"]})


@bp.route("/api/media/<job_id>")
def api_media(job_id):
    """Исходник для плеера (<audio>/<video>) — inline, с поддержкой range.

    Параметр a=<index> отдаёт версию с выбранной аудиодорожкой (быстрый ремукс
    без перекодирования) — чтобы в предпросмотре звучала именно она."""
    a = request.args.get("a", type=int) or 0
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("src_path"):
            abort(404)
        src = Path(job["src_path"])
    if not src.is_file():
        abort(404)

    if a == 0:
        return send_file(str(src), as_attachment=False, conditional=True)

    out = src.parent / f"preview_a{a}{src.suffix.lower()}"
    if not out.is_file():
        maps = ["-map", "0:v:0?", "-map", f"0:a:{a}"]
        extra = (["-movflags", "+faststart"]
                 if src.suffix.lower() == ".mp4" else [])
        try:
            subprocess.run(
                [core.FFMPEG_BIN, "-v", "error", "-y", "-i", str(src)] + maps
                + ["-c", "copy"] + extra + [str(out)],
                capture_output=True, timeout=300, creationflags=core._NO_WINDOW,
            )
        except Exception:
            abort(500)
        if not out.is_file():
            abort(500)
    return send_file(str(out), as_attachment=False, conditional=True)


@bp.route("/api/peaks/<job_id>")
def api_peaks(job_id):
    """Волна (огибающая) выбранной аудиодорожки — для обновления при смене дорожки."""
    a = request.args.get("a", type=int) or 0
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("src_path"):
            abort(404)
        src = Path(job["src_path"])
    if not src.is_file():
        abort(404)
    return jsonify({"peaks": _generate_peaks(src, a)})


@bp.route("/api/filmstrip/<job_id>")
def api_filmstrip(job_id):
    """Полоса из n кадров для участка [a, b] (под текущий зум). С кэшем на диске."""
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("src_path"):
            abort(404)
        src = Path(job["src_path"])
        duration = float(job.get("duration") or 0)

    a = request.args.get("a", type=float)
    b = request.args.get("b", type=float)
    n = request.args.get("n", type=int)
    if a is None or b is None or not n:
        abort(404)
    a = max(0.0, a)
    b = min(b, duration) if duration else b
    if b - a < 0.05:
        abort(404)
    n = max(1, min(n, 160))

    out = src.parent / f"strip_{a:.1f}_{b:.1f}_{n}.jpg"
    if not out.is_file() and not _make_filmstrip(src, out, a, b, n):
        abort(500)
    return send_file(str(out), as_attachment=False, conditional=True)


@bp.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    out_mode = (data.get("mode") or "audio").strip()
    out_format = (data.get("format") or "mp3").strip()
    audio_index = int(data.get("audio") or 0)
    do_compress = bool(data.get("compress"))
    try:
        target_mb = float(data.get("target_mb") or 0)
    except (TypeError, ValueError):
        target_mb = 0
    c_height = int(data.get("resolution") or 0)     # 0 = исходное
    audio_kbps = int(data.get("audio_kbps") or 128)
    try:
        start = float(data.get("start"))
        end = float(data.get("end"))
    except (TypeError, ValueError):
        return jsonify({"error": "Неверные тайминги."}), 400

    if out_mode not in ("audio", "video"):
        return jsonify({"error": "Неизвестный режим."}), 400
    if out_mode == "audio" and out_format not in AUDIO_FORMATS:
        return jsonify({"error": "Неподдерживаемый аудиоформат."}), 400
    dep_err = core.missing_dependencies_payload("trim")
    if dep_err:
        return jsonify(dep_err), 400
    temp_err = core.temp_blocked_error()
    if temp_err:
        return jsonify({"error": temp_err}), 400

    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or not job.get("src_path"):
            return jsonify({"error": "Источник не готов. Загрузите заново."}), 404
        if core.is_job_active(job):
            return jsonify({"error": "Эта задача уже выполняется."}), 409
        if out_mode == "video" and not job.get("has_video"):
            return jsonify({"error": "В источнике нет видео."}), 400

        duration = float(job.get("duration") or 0)
        start = max(0.0, start)
        end = min(end, duration) if duration else end
        if end - start < 0.05:
            return jsonify({"error": "Слишком короткий фрагмент."}), 400

        tracks = job.get("audio_tracks") or []
        if tracks and not (0 <= audio_index < len(tracks)):
            audio_index = 0

        compress = do_compress and out_mode == "video"
        if compress and target_mb <= 0:
            return jsonify({"error": "Укажите целевой размер (МБ) для сжатия."}), 400

        job.update({
            "status": "pending", "progress": 0.0, "error": "", "stage": "",
            "filename": "", "download_name": "", "result_size_mb": None,
            "start": start, "end": end,
            "out_mode": out_mode, "out_format": out_format,
            "audio_index": audio_index,
            "compress": compress, "target_mb": target_mb,
            "c_height": c_height, "audio_kbps": audio_kbps,
        })

    t = threading.Thread(target=_trim_thread, args=(job_id, job), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/result/<job_id>")
def api_result(job_id):
    """Отдаёт нарезанный фрагмент. Уборку не форсируем — чтобы из одного
    источника можно было нарезать несколько фрагментов (оригинал доживёт до
    авто-очистки по таймауту)."""
    with core.JOBS_LOCK:
        job = core.JOBS.get(job_id)
        if not job or job.get("status") != "done" or not job.get("filename"):
            abort(404)
        path = job["filename"]
        dl_name = job["download_name"]
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=dl_name)
