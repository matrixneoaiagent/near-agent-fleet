"""Media analysis and rendering for the Video Clipping Agent.

Everything here is local: ffmpeg/ffprobe for decoding, encoding and scene
detection, and faster-whisper for speech-to-text. No cloud API is called.
"""

from __future__ import annotations

import array
import glob
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Iterable

try:  # numpy makes frame/audio maths much faster, but is optional.
    import numpy as np
except Exception:  # pragma: no cover
    np = None


class MediaError(RuntimeError):
    pass


class CaptionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tool discovery


def _find_tool(name: str) -> str:
    env = os.environ.get(f"{name.upper()}_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which(name)
    if found:
        return found
    patterns = [
        f"/nix/store/*ffmpeg*-bin/bin/{name}",
        f"/nix/store/*ffmpeg*/bin/{name}",
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
    ]
    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    raise MediaError(
        f"{name} not found. Set {name.upper()}_BIN or install ffmpeg."
    )


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise MediaError(f"command failed ({proc.returncode}): {' '.join(cmd[:4])} ... {err[-1200:]}")
    return proc


def _fraction(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# Probing and source acquisition


def probe(path: str) -> dict[str, Any]:
    proc = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        timeout=120,
    )
    data = json.loads(proc.stdout.decode("utf-8", "replace"))
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        pass
    if duration <= 0 and video:
        try:
            duration = float(video.get("duration") or 0)
        except Exception:
            pass
    if not video:
        raise MediaError("source has no video stream")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    # ffmpeg applies rotation when decoding; ffprobe reports the coded size.
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                rotation = int(float(side["rotation"]))
            except Exception:
                pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate")) or 30.0,
        "has_audio": audio is not None,
        "video_codec": video.get("codec_name"),
        "audio_codec": (audio or {}).get("codec_name"),
        "size": int(fmt.get("size") or 0),
        "format_name": fmt.get("format_name"),
    }


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return name[:80] or "video"


def download_direct(url: str, dest_dir: str, max_bytes: int = 2 * 1024**3) -> str:
    """Download a direct media URL with a hard size cap."""
    import urllib.request
    import urllib.error

    os.makedirs(dest_dir, exist_ok=True)
    parsed = url.split("?", 1)[0]
    ext = os.path.splitext(parsed)[1].lower()
    if ext not in {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".ogv", ".mpg", ".mpeg", ".ts"}:
        ext = ".mp4"
    dest = os.path.join(dest_dir, "source" + ext)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; VideoClippingAgent/1.0)",
            "Accept": "video/*,application/octet-stream,*/*",
        },
    )
    total = 0
    with urllib.request.urlopen(req, timeout=60) as res, open(dest, "wb") as out:
        while True:
            chunk = res.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise MediaError(f"source is larger than the {max_bytes // (1024**3)} GiB limit")
            out.write(chunk)
    if total < 1000:
        raise MediaError("downloaded source is empty")
    return dest


def download_with_ytdlp(
    url: str,
    dest_dir: str,
    max_height: int = 720,
    cookies_file: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Download a public page URL (YouTube, Vimeo, ...) and return info."""
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise MediaError("yt-dlp is not installed in this runtime") from exc

    os.makedirs(dest_dir, exist_ok=True)
    outtmpl = os.path.join(dest_dir, "source.%(ext)s")
    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 60,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": True,
        "overwrites": True,
        "ffmpeg_location": os.path.dirname(FFMPEG),
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as exc:
        raise MediaError(f"could not download the public video page: {exc}") from exc
    # merge_output_format can change the extension
    if not os.path.exists(path):
        candidates = sorted(glob.glob(os.path.join(dest_dir, "source.*")))
        candidates = [c for c in candidates if not c.endswith(".json") and not c.endswith(".part")]
        if not candidates:
            raise MediaError("yt-dlp finished but no media file was produced")
        path = candidates[0]
    return path, info


def download_source(
    url: str,
    dest_dir: str,
    max_height: int = 720,
    cookies_file: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Download a source URL. Page URLs use yt-dlp; direct files use urllib."""
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower()
    page_hosts = (
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "vimeo.com",
        "dailymotion.com",
        "twitch.tv",
        "soundcloud.com",
    )
    is_page = any(host == h or host.endswith("." + h) for h in page_hosts)
    if is_page:
        return download_with_ytdlp(url, dest_dir, max_height=max_height, cookies_file=cookies_file)
    # HLS manifests are best handled by ffmpeg.
    if ".m3u8" in url.lower():
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "source.mp4")
        _run(
            [
                FFMPEG,
                "-v",
                "error",
                "-y",
                "-i",
                url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                dest,
            ],
            timeout=1800,
        )
        return dest, {"title": os.path.basename(url), "webpage_url": url}
    return download_direct(url, dest_dir), {"title": os.path.basename(url.split("?")[0]), "webpage_url": url}


def extract_heatmap(info: dict[str, Any], page_url: str | None = None) -> list[dict[str, float]]:
    """Return [{start,end,value}] from yt-dlp or the YouTube watch page."""
    raw = info.get("heatmap") if isinstance(info, dict) else None
    if isinstance(raw, list) and raw:
        out = []
        for row in raw:
            try:
                out.append(
                    {
                        "start": float(row.get("start_time", row.get("start", 0))),
                        "end": float(row.get("end_time", row.get("end", 0))),
                        "value": float(row.get("value", row.get("heat", 0))),
                    }
                )
            except Exception:
                continue
        if out:
            return out

    if not page_url:
        return []
    # Best-effort scrape of the public watch page. YouTube does not expose a
    # documented retention API, so this is opportunistic and never claimed
    # when absent.
    try:
        import urllib.request

        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            html = res.read(4_000_000).decode("utf-8", "replace")
    except Exception:
        return []

    for key in ("heatMarkers", "heatmap"):
        idx = html.find(f'"{key}"')
        if idx < 0:
            continue
        start = html.find("[", idx)
        if start < 0:
            continue
        depth = 0
        end = -1
        for pos in range(start, min(len(html), start + 2_000_000)):
            ch = html[pos]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        if end < 0:
            continue
        try:
            data = json.loads(html[start:end])
        except Exception:
            continue
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            r = row.get("heatMarkerRenderer") or row
            try:
                start_ms = float(r.get("timeRangeStartMillis", r.get("startMillis", 0)))
                dur_ms = float(r.get("markerDurationMillis", r.get("durationMillis", 0)))
                value = float(
                    r.get(
                        "heatMarkerIntensityScoreNormalized",
                        r.get("intensityScoreNormalized", r.get("value", 0)),
                    )
                )
            except Exception:
                continue
            out.append({"start": start_ms / 1000.0, "end": (start_ms + dur_ms) / 1000.0, "value": value})
        if out:
            return out
    return []


# --------------------------------------------------------------------------
# Analysis primitives


def audio_energy(path: str, bin_seconds: float = 0.5, sample_rate: int = 8000) -> list[float]:
    """RMS energy per bin, normalised to roughly 0..1."""
    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bin_bytes = int(sample_rate * 2 * bin_seconds)
    values: list[float] = []
    try:
        while True:
            chunk = proc.stdout.read(bin_bytes)  # type: ignore[union-attr]
            if not chunk:
                break
            if len(chunk) % 2:
                chunk = chunk[:-1]
            if not chunk:
                continue
            if np is not None:
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            else:
                arr = array.array("h")
                arr.frombytes(chunk)
                total = sum(float(x) * float(x) for x in arr) / max(1, len(arr))
                rms = math.sqrt(total) / 32768.0
            values.append(rms)
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:
            pass
        err = proc.stderr.read() if proc.stderr else b""  # type: ignore[union-attr]
        proc.wait(timeout=30)
    if proc.returncode not in (0,):
        raise MediaError(f"audio decode failed: {err.decode('utf-8', 'replace')[-800:]}")
    return values


def detect_silences(
    path: str, noise_db: float = -32.0, min_silence: float = 0.28
) -> list[tuple[float, float]]:
    """Return [(start,end)] silence intervals, trying a looser threshold if needed."""
    for db in (noise_db, noise_db + 6, noise_db + 12):
        cmd = [
            FFMPEG,
            "-v",
            "info",
            "-i",
            path,
            "-vn",
            "-af",
            f"silencedetect=noise={db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
        text = proc.stderr.decode("utf-8", "replace")
        starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]
        out: list[tuple[float, float]] = []
        for i, start in enumerate(starts):
            end = ends[i] if i < len(ends) else None
            if end is None:
                continue
            if end > start:
                out.append((start, end))
        if len(out) >= 3 or db >= noise_db + 12:
            return out
    return []


def scene_cuts(path: str, threshold: float = 0.18) -> list[tuple[float, float]]:
    """Return [(time, score)] scene-change events."""
    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-i",
        path,
        "-an",
        "-vf",
        f"scale=320:-2,select='gt(scene,{threshold})',metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
    text = proc.stdout.decode("utf-8", "replace")
    out: list[tuple[float, float]] = []
    pending_time: float | None = None
    for line in text.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            pending_time = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if m and pending_time is not None:
            out.append((pending_time, float(m.group(1))))
            pending_time = None
    if proc.returncode != 0 and not out:
        err = proc.stderr.decode("utf-8", "replace")[-600:]
        raise MediaError(f"scene detection failed: {err}")
    return out


def motion_series(path: str, fps: float = 2.0, width: int = 160, height: int = 90) -> list[float]:
    """Mean absolute frame difference at `fps`, normalised to 0..1."""
    frame_size = width * height
    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-i",
        path,
        "-an",
        "-vf",
        f"fps={fps},scale={width}:{height},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    values: list[float] = []
    prev = None
    try:
        while True:
            frame = proc.stdout.read(frame_size)  # type: ignore[union-attr]
            if len(frame) < frame_size:
                break
            if np is not None:
                arr = np.frombuffer(frame, dtype=np.uint8).astype(np.int16)
                if prev is not None:
                    values.append(float(np.mean(np.abs(arr - prev))) / 255.0)
                prev = arr
            else:
                if prev is not None:
                    values.append(sum(abs(a - b) for a, b in zip(frame, prev)) / (frame_size * 255.0))
                prev = frame
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:
            pass
        err = proc.stderr.read() if proc.stderr else b""  # type: ignore[union-attr]
        proc.wait(timeout=30)
    if proc.returncode != 0 and not values:
        raise MediaError(f"motion analysis failed: {err.decode('utf-8', 'replace')[-600:]}")
    return values


def extract_audio_segment(path: str, start: float, duration: float, out_wav: str) -> None:
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    _run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.1, duration):.3f}",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            out_wav,
        ],
        timeout=600,
    )


# --------------------------------------------------------------------------
# Whisper transcription


_model_lock = threading.Lock()
_model_cache: dict[str, Any] = {}


def get_whisper_model(size: str | None = None):
    size = size or os.environ.get("WHISPER_MODEL", "medium")
    with _model_lock:
        if size in _model_cache:
            return _model_cache[size]
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            raise CaptionError(
                "faster-whisper is not installed; captions are unavailable in this runtime"
            ) from exc
        root = os.environ.get(
            "WHISPER_MODEL_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
        )
        os.makedirs(root, exist_ok=True)
        try:
            model = WhisperModel(
                size,
                device="cpu",
                compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
                download_root=root,
                cpu_threads=int(os.environ.get("WHISPER_THREADS", "4")),
            )
        except Exception as exc:
            if size != "small":
                try:
                    model = WhisperModel(
                        "small",
                        device="cpu",
                        compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
                        download_root=root,
                        cpu_threads=int(os.environ.get("WHISPER_THREADS", "4")),
                    )
                    size = "small"
                except Exception:
                    raise CaptionError(f"could not load the local Whisper model: {exc}") from exc
            else:
                raise CaptionError(f"could not load the local Whisper model: {exc}") from exc
        _model_cache[size] = model
        return model


def transcribe_wav(wav_path: str, language: str | None = None, model_size: str | None = None) -> dict[str, Any]:
    try:
        model = get_whisper_model(model_size)
    except CaptionError:
        raise
    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise CaptionError("unexpected WAV sample width")
    if np is None:
        raise CaptionError("numpy is required for local transcription")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if audio.size < rate * 0.2:
        return {"text": "", "segments": [], "words": [], "language": language, "duration": audio.size / rate}
    try:
        segments_iter, info = model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            word_timestamps=True,
            beam_size=5,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        segments = []
        words = []
        text_parts = []
        for seg in segments_iter:
            seg_words = []
            for w in (seg.words or []):
                if w.start is None or w.end is None:
                    continue
                word = {
                    "start": float(w.start),
                    "end": float(w.end),
                    "text": (w.word or "").strip(),
                    "probability": float(w.probability or 0),
                }
                if word["text"]:
                    words.append(word)
                    seg_words.append(word)
            clean = (seg.text or "").strip()
            if clean:
                text_parts.append(clean)
            segments.append(
                {
                    "start": float(seg.start or 0),
                    "end": float(seg.end or 0),
                    "text": clean,
                    "words": seg_words,
                }
            )
        return {
            "text": " ".join(text_parts).strip(),
            "segments": segments,
            "words": words,
            "language": getattr(info, "language", language) or language,
            "language_probability": float(getattr(info, "language_probability", 0) or 0),
            "duration": float(getattr(info, "duration", audio.size / rate) or 0),
        }
    except Exception as exc:
        raise CaptionError(f"local transcription failed: {exc}") from exc


def detect_language(path: str, work_dir: str | None = None, sample_seconds: float = 20.0) -> str | None:
    """Detect the spoken language once per source, so word timestamps are consistent."""
    try:
        info = probe(path)
        duration = float(info.get("duration") or 0)
        if duration <= 0:
            return None
        start = max(0.0, min(duration * 0.25, duration - sample_seconds - 0.5))
        work_dir = work_dir or tempfile.mkdtemp(prefix="vca-lang-")
        os.makedirs(work_dir, exist_ok=True)
        wav = os.path.join(work_dir, "language-sample.wav")
        extract_audio_segment(path, start, min(sample_seconds, duration - start), wav)
        with wave.open(wav, "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if np is None:
            return None
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        if audio.size < rate * 1.0:
            return None
        model = get_whisper_model()
        _, info2 = model.transcribe(
            audio,
            language=None,
            vad_filter=False,
            without_timestamps=True,
            beam_size=1,
            condition_on_previous_text=False,
        )
        lang = getattr(info2, "language", None)
        return str(lang) if lang else None
    except Exception:
        return None


def transcribe_range(
    path: str,
    start: float,
    end: float,
    language: str | None = None,
    model_size: str | None = None,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Transcribe [start,end] with a little padding, returning absolute times."""
    pad = 1.5
    s = max(0.0, start - pad)
    e = max(s + 0.2, end + pad)
    work_dir = work_dir or tempfile.mkdtemp(prefix="vca-asr-")
    os.makedirs(work_dir, exist_ok=True)
    wav = os.path.join(work_dir, f"asr-{int(start*1000)}-{int(end*1000)}.wav")
    extract_audio_segment(path, s, e - s, wav)
    result = transcribe_wav(wav, language=language, model_size=model_size)
    words = []
    for w in result.get("words", []):
        words.append({**w, "start": w["start"] + s, "end": w["end"] + s})
    segments = []
    for seg in result.get("segments", []):
        segments.append(
            {
                **seg,
                "start": seg["start"] + s,
                "end": seg["end"] + s,
                "words": [
                    {**w, "start": w["start"] + s, "end": w["end"] + s}
                    for w in seg.get("words", [])
                ],
            }
        )
    result["words"] = words
    result["segments"] = segments
    result["offset"] = s
    return result


# --------------------------------------------------------------------------
# Captions


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def group_words_into_cues(
    words: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
    max_words: int = 7,
    max_chars: int = 44,
    pause_break: float = 0.45,
) -> list[dict[str, Any]]:
    """Group word timestamps into short, complete-word caption cues."""
    usable = [
        w
        for w in words
        if w.get("text")
        and w.get("end", 0) > clip_start + 0.05
        and w.get("start", 0) < clip_end - 0.05
    ]
    usable.sort(key=lambda w: w["start"])
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        start = max(clip_start, current[0]["start"])
        end = min(clip_end, current[-1]["end"] + 0.18)
        if end - start < 0.25:
            end = min(clip_end, start + 0.6)
        text = " ".join(w["text"] for w in current)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append({"start": start, "end": end, "text": text})
        current = []
        current_chars = 0

    for w in usable:
        add_len = len(w["text"]) + (1 if current else 0)
        break_pause = bool(current) and (w["start"] - current[-1]["end"] > pause_break)
        break_sentence = bool(current) and bool(re.search(r"[.!?]$", current[-1]["text"]))
        if current and (len(current) >= max_words or current_chars + add_len > max_chars or break_pause or break_sentence):
            flush()
        if not current and w["start"] < clip_start:
            # keep the word, clamp the cue start later
            pass
        current.append(w)
        current_chars += add_len
    flush()
    # de-overlap
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"] - 0.02:
            cues[i]["end"] = max(cues[i]["start"] + 0.3, cues[i + 1]["start"] - 0.02)
    return cues


def make_ass(cues: list[dict[str, Any]], clip_start: float, fmt: str, out_path: str) -> str:
    if fmt == "9:16":
        w, h, font, margin_v, margin_h, max_chars, max_words = 1080, 1920, 68, 250, 70, 40, 7
    elif fmt == "1:1":
        w, h, font, margin_v, margin_h, max_chars, max_words = 1080, 1080, 54, 185, 70, 46, 8
    else:
        w, h, font, margin_v, margin_h, max_chars, max_words = 1920, 1080, 52, 75, 100, 62, 10
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,DejaVu Sans,{font},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,{margin_h},{margin_h},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        start = max(0.0, cue["start"] - clip_start)
        end = max(start + 0.25, cue["end"] - clip_start)
        text = _ass_escape(cue["text"])
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return out_path


def make_srt(cues: list[dict[str, Any]], clip_start: float, out_path: str) -> str:
    def ts(sec: float) -> str:
        ms = int(round(max(0.0, sec) * 1000))
        h, rem = divmod(ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, cue in enumerate(cues, 1):
        start = max(0.0, cue["start"] - clip_start)
        end = max(start + 0.25, cue["end"] - clip_start)
        lines += [str(i), f"{ts(start)} --> {ts(end)}", cue["text"], ""]
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return out_path


def _font_dir() -> str | None:
    fc = shutil.which("fc-match")
    if not fc:
        return None
    try:
        proc = subprocess.run(
            [fc, "-f", "%{file}", "DejaVu Sans"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        path = proc.stdout.decode().strip()
        return os.path.dirname(path) if path and os.path.exists(path) else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Rendering


CANVAS = {
    "9:16": ((1080, 1920), (720, 1280), (540, 960)),
    "1:1": ((1080, 1080), (720, 720), (540, 540)),
    "16:9": ((1920, 1080), (1280, 720), (960, 540)),
}


def choose_resolution(fmt: str, total_seconds: float, budget_bytes: int = 92 * 1024**2) -> tuple[int, int, int]:
    """Pick a canvas and a per-output max bitrate that fits the upload budget."""
    total_seconds = max(1.0, total_seconds)
    bits = budget_bytes * 8 * 0.86
    per_second = bits / total_seconds
    for idx, (w, h) in enumerate(CANVAS[fmt]):
        if idx == 0 and per_second >= 2_000_000:
            return w, h, int(min(5_000_000, per_second))
        if idx == 1 and per_second >= 850_000:
            return w, h, int(min(3_000_000, per_second))
        if idx == 2:
            return w, h, int(max(600_000, min(2_000_000, per_second)))
    w, h = CANVAS[fmt][-1]
    return w, h, int(max(600_000, min(2_000_000, per_second)))


def _aspect(w: int, h: int) -> float:
    return w / h if h else 0.0


def render_clip(
    source: str,
    start: float,
    end: float,
    fmt: str,
    out_path: str,
    width: int,
    height: int,
    maxrate: int,
    ass_path: str | None = None,
    has_audio: bool = True,
) -> dict[str, Any]:
    duration = max(0.2, end - start)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    src = probe(source)
    target_aspect = _aspect(width, height)
    source_aspect = _aspect(src["width"], src["height"])
    same = abs(target_aspect - source_aspect) / max(0.01, target_aspect) < 0.06

    if same:
        base = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v0]"
        )
    else:
        base = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=28:2,eq=brightness=-0.28:saturation=0.70[bg2];"
            f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
        )
    if ass_path:
        fonts = _font_dir()
        sub = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        sub_filter = f"subtitles=filename='{sub}'"
        if fonts:
            fdir = fonts.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            sub_filter += f":fontsdir='{fdir}'"
        graph = f"{base};[v0]{sub_filter}[v]"
    else:
        graph = f"{base};[v0]null[v]"

    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        source,
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        graph,
        "-map",
        "[v]",
    ]
    if has_audio:
        cmd += ["-map", "0:a?", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-maxrate",
        str(maxrate),
        "-bufsize",
        str(maxrate * 2),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-r",
        "30",
        "-movflags",
        "+faststart",
        "-threads",
        "4",
        out_path,
    ]
    _run(cmd, timeout=3600)
    info = probe(out_path)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 2000:
        raise MediaError("render produced an empty file")
    return {
        "path": out_path,
        "width": info["width"],
        "height": info["height"],
        "duration": info["duration"],
        "size": os.path.getsize(out_path),
        "maxrate": maxrate,
    }
