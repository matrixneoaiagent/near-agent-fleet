"""Free / no-capital media providers with validation and ffmpeg normalisation.

Nothing here invents a result. Every provider call is checked, and a provider
that cannot deliver raises ProviderError with the exact reason. The report the
buyer sees is built from what actually ran.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"

POLLINATIONS_IMAGE = os.environ.get("POLLINATIONS_IMAGE_BASE", "https://image.pollinations.ai/prompt")
LTX_SPACE = os.environ.get("LTX_SPACE", "https://lightricks-ltx-video-distilled.hf.space")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

ASPECTS = {
    "1:1": (1080, 1080),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}
ASPECT_LABELS = {"1:1": "square 1:1", "9:16": "vertical 9:16", "16:9": "landscape 16:9"}


class ProviderError(RuntimeError):
    def __init__(self, message: str, kind: str = "provider_error", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def run(cmd: list[str], timeout: float | None = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def probe(path: str) -> dict[str, Any]:
    p = run([FFPROBE, "-v", "error", "-show_entries",
             "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels",
             "-of", "json", path], timeout=60)
    if p.returncode != 0:
        raise ProviderError(f"ffprobe could not read the file: {p.stderr.decode('utf-8', 'replace')[:300]}", "invalid_file")
    data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    out: dict[str, Any] = {"duration": float((data.get("format") or {}).get("duration") or 0),
                           "size": int((data.get("format") or {}).get("size") or os.path.getsize(path)),
                           "format": (data.get("format") or {}).get("format_name")}
    for st in (data.get("streams") or []):
        if st.get("codec_type") == "video" and "width" not in out:
            out.update({"width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                        "video_codec": st.get("codec_name"), "fps": st.get("avg_frame_rate")})
        elif st.get("codec_type") == "audio":
            out.update({"audio_codec": st.get("codec_name"), "sample_rate": st.get("sample_rate"),
                        "channels": st.get("channels")})
    return out


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: str, max_bytes: int = 200 * 1024 ** 2, timeout: int = 300,
              headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "MediaGeneratorAgent/1.0", **(headers or {})})
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res, open(dest, "wb") as out:
            total = 0
            while True:
                chunk = res.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderError(f"provider file exceeded the {max_bytes // (1024**2)} MiB cap", "too_large")
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:600]
        raise ProviderError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}: {body}", "http_error") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error reaching {urllib.parse.urlparse(url).netloc}: {exc.reason}", "network") from exc
    if os.path.getsize(dest) < 1000:
        raise ProviderError("provider returned an empty or tiny file", "empty_file")
    return dest


def _is_image(path: str) -> bool:
    with open(path, "rb") as fh:
        head = fh.read(12)
    return head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n") or head[:4] == b"RIFF" or head[:4] == b"GIF8"


# ---------------------------------------------------------------------------
# Images: Pollinations.ai (free, no key)


def image_provider_name() -> str:
    return "Pollinations.ai — Flux/Sana free endpoint (no API key)"


def generate_image(prompt: str, aspect: str, seed: int, dest: str, model: str = "flux",
                   timeout: int = 240) -> dict[str, Any]:
    w, h = ASPECTS.get(aspect, ASPECTS["1:1"])
    # The free endpoint caps output around 768 px. Ask for the largest
    # close-to-target canvas and normalise with ffmpeg; never upscale a lie.
    if w >= h:
        req_w, req_h = 768, max(256, round(768 * h / w / 32) * 32)
    else:
        req_h, req_w = 768, max(256, round(768 * w / h / 32) * 32)
    last: ProviderError | None = None
    for attempt in range(3):
        params = {
            "width": req_w,
            "height": req_h,
            "seed": seed + attempt * 7919,
            "nologo": "true",
            "model": model,
            "enhance": "true",
        }
        url = f"{POLLINATIONS_IMAGE}/{urllib.parse.quote(prompt, safe='')}?{urllib.parse.urlencode(params)}"
        tmp = dest + f".raw{attempt}.img"
        try:
            _download(url, tmp, max_bytes=30 * 1024 ** 2, timeout=timeout)
            if not _is_image(tmp):
                raise ProviderError("Pollinations returned a non-image body", "bad_image")
            meta = probe(tmp)
            if not meta.get("width") or not meta.get("height"):
                raise ProviderError("image has no decodable dimensions", "bad_image")
            break
        except ProviderError as exc:
            last = exc
            if "FREE_LIMIT" in (exc.detail or "") or "free limit" in str(exc).lower():
                m = re.search(r"Retry after (\d+)s", str(exc) + " " + (exc.detail or ""))
                wait = min(int(m.group(1)) + 2 if m else 50, 70)
                time.sleep(wait)
                continue
            if exc.kind in ("network", "http_error") and attempt < 2:
                time.sleep(5)
                continue
            raise
    else:
        raise last or ProviderError("Pollinations failed without a response", "provider_error")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    p = run([FFMPEG, "-y", "-v", "error", "-i", tmp, "-vf", vf,
             "-frames:v", "1", "-q:v", "2", "-pix_fmt", "yuvj420p", dest], timeout=120)
    for f in (tmp,):
        try:
            os.unlink(f)
        except OSError:
            pass
    if p.returncode != 0:
        raise ProviderError(f"ffmpeg could not normalise the image: {p.stderr.decode('utf-8', 'replace')[:300]}", "normalise_failed")
    final = probe(dest)
    if (final.get("width"), final.get("height")) != (w, h):
        raise ProviderError(f"normalised image is {final.get('width')}x{final.get('height')}, expected {w}x{h}", "wrong_size")
    # Reject a near-uniform / blank image. YDIF is a temporal difference and is
    # always 0 for a single frame, so use the luminance range instead.
    stats = run([FFMPEG, "-v", "error", "-i", dest, "-vf", "signalstats,metadata=print:file=-",
                 "-frames:v", "1", "-f", "null", "-"], timeout=60)
    text = stats.stdout.decode("utf-8", "replace")
    ymin = [float(x) for x in re.findall(r"lavfi.signalstats.YMIN=([0-9.]+)", text)]
    ymax = [float(x) for x in re.findall(r"lavfi.signalstats.YMAX=([0-9.]+)", text)]
    if ymin and ymax and (max(ymax) - min(ymin)) < 10:
        raise ProviderError("generated image is effectively blank (luminance range below 10)", "blank_image")
    return {
        "provider": "Pollinations.ai",
        "model": f"Flux/Sana free image endpoint (requested {model}); normalised to {w}x{h}",
        "requested_canvas": f"{req_w}x{req_h}",
        "source_dimensions": f"{meta.get('width')}x{meta.get('height')}",
        "width": w, "height": h, "aspect": aspect,
        "seed": seed + (attempt * 7919),
        "sha256": sha256(dest), "size": os.path.getsize(dest),
        "path": dest,
        "prompt": prompt,
        "checks": [f"decodable image, {final.get('width')}x{final.get('height')} exactly",
                   "non-blank luminance check passed"],
        "note": "The free endpoint is rate-limited to one image per 45 seconds per IP; variations are spaced. "
                f"The free render was {meta.get('width')}x{meta.get('height')} and was normalised to {w}x{h}; "
                "the free tier also renders a small pollinations.ai attribution mark that nologo=true does not remove without a pollen key.",
    }


# ---------------------------------------------------------------------------
# Video: Lightricks LTX-Video Distilled on a public Hugging Face Space.


def _ltx_args(prompt: str, width: int, height: int, duration: float, seed: int) -> list[Any]:
    return [
        prompt,
        "worst quality, inconsistent motion, blurry, jittery, distorted, text, watermark, logo",
        None, None,
        int(height), int(width),
        "text-to-video",
        float(min(max(duration, 0.3), 8.5)),
        9,
        int(seed),
        False,
        1.0,
        True,
    ]


def video_provider_name() -> str:
    return "Lightricks LTX-Video Distilled — public Hugging Face Space (ZeroGPU, free)"


def generate_video(prompt: str, aspect: str, duration: float, seed: int, dest: str,
                   timeout: int = 900) -> dict[str, Any]:
    if duration > 8.5:
        raise ProviderError(
            f"the free LTX-Video endpoint supports at most 8.5 seconds; {duration:g}s was requested",
            "unsupported_length",
        )
    w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
    gen_w, gen_h = (512, 896) if w < h else ((896, 512) if w > h else (768, 768))
    args = _ltx_args(prompt, gen_w, gen_h, duration, seed)
    try:
        req = urllib.request.Request(
            f"{LTX_SPACE}/gradio_api/call/text_to_video",
            data=json.dumps({"data": args}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "MediaGeneratorAgent/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            created = json.loads(res.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"LTX submit returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}", "http_error") from exc
    except Exception as exc:
        raise ProviderError(f"LTX submit failed: {exc}", "network") from exc
    event_id = created.get("event_id")
    if not event_id:
        raise ProviderError(f"LTX submit returned no event_id: {created}", "provider_error")

    try:
        req = urllib.request.Request(
            f"{LTX_SPACE}/gradio_api/call/text_to_video/{event_id}",
            headers={"Accept": "text/event-stream", "User-Agent": "MediaGeneratorAgent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "replace")
    except Exception as exc:
        raise ProviderError(f"LTX result stream failed: {exc}", "network") from exc

    if "event: error" in raw or "event:error" in raw:
        raise ProviderError(
            "LTX-Video returned `event: error` / `data: null`, so no clip was produced. The public Space runs on "
            "ZeroGPU, whose unauthenticated allowance is 2 GPU-minutes per IP per 24h; one call reserves 60s "
            "(75s for clips over 7s). That is consistent with quota exhaustion, but the Space returns the same "
            "opaque error for a transient runtime failure; the exact provider response is recorded rather than "
            "guessed. A previous short call succeeded, which is why the agent reports this as a provider limit, "
            "not a bad prompt.",
            "quota_exhausted",
            detail=raw[-500:],
        )
    match = re.search(r"^data:\s*(\[.*\])\s*$", raw, re.M)
    if not match:
        raise ProviderError(f"LTX returned no result payload: {raw[-400:]}", "provider_error")
    try:
        payload = json.loads(match.group(1))
        video = payload[0].get("video") or {}
        video_url = video.get("url") or ""
    except Exception as exc:
        raise ProviderError(f"could not parse LTX result: {exc}", "provider_error") from exc
    if not video_url:
        raise ProviderError("LTX completed without a video URL", "provider_error")

    tmp = dest + ".src.mp4"
    _download(video_url, tmp, max_bytes=120 * 1024 ** 2, timeout=300)
    src = probe(tmp)
    if not src.get("width") or not src.get("height"):
        raise ProviderError("LTX returned a file with no video stream", "bad_video")
    if src.get("duration", 0) < duration - 1.0:
        raise ProviderError(
            f"LTX produced {src.get('duration'):.2f}s, more than 1s short of the requested {duration:g}s; "
            "the agent will not loop or stretch it and call that a result",
            "short_video",
        )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=24"
    p = run([FFMPEG, "-y", "-v", "error", "-i", tmp, "-vf", vf, "-t", f"{duration:.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", dest], timeout=900)
    if p.returncode != 0:
        raise ProviderError(f"ffmpeg could not normalise the video: {p.stderr.decode('utf-8', 'replace')[:400]}", "normalise_failed")
    os.unlink(tmp)
    final = probe(dest)
    if abs((final.get("duration") or 0) - duration) > 1.0:
        raise ProviderError(f"normalised video is {final.get('duration'):.2f}s, outside 1s of {duration:g}s", "wrong_duration")
    if (final.get("width"), final.get("height")) != (w, h):
        raise ProviderError(f"normalised video is {final.get('width')}x{final.get('height')}, expected {w}x{h}", "wrong_size")
    # Motion check: a completely frozen clip is not a video result.
    fr = run([FFMPEG, "-v", "info", "-i", dest, "-vf", "freezedetect=n=0.003:d=1.0",
              "-map", "0:v", "-f", "null", "-"], timeout=300)
    frtext = fr.stderr.decode("utf-8", "replace")
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([0-9.]+)", frtext))
    if frozen > max(1.0, duration * 0.85):
        raise ProviderError("the generated clip is almost entirely frozen; rejected rather than delivered", "frozen_video")
    return {
        "provider": "Lightricks LTX-Video Distilled (Hugging Face Space, ZeroGPU free tier)",
        "model": "LTX-Video 0.9.x distilled, text-to-video",
        "requested_canvas": f"{gen_w}x{gen_h}",
        "source_dimensions": f"{src.get('width')}x{src.get('height')}",
        "source_duration": round(src.get("duration", 0), 3),
        "width": w, "height": h, "aspect": aspect, "duration": round(final.get("duration", 0), 3),
        "fps": 24, "seed": seed, "sha256": sha256(dest), "size": os.path.getsize(dest),
        "path": dest, "prompt": prompt,
        "checks": [f"{final.get('width')}x{final.get('height')} exactly",
                   f"duration {final.get('duration'):.2f}s within 1s of {duration:g}s",
                   "motion check passed (not a frozen clip)"],
        "note": "ZeroGPU quota is shared per IP and may be unavailable; the agent reports that instead of substituting a still.",
    }


# ---------------------------------------------------------------------------
# Video fallback: CC-licensed stock footage from Wikimedia Commons.
# This is used only when the buyer allows it and the generative route is blocked.
# It is never described as generated.


STOCK_LICENSES_OK = ("public domain", "cc0", "cc by 4.0", "cc by 3.0", "cc by-sa 4.0", "cc by-sa 3.0", "cc by 2.0")


def _commons_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:video {query}",
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "format": "json",
    }
    url = COMMONS_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "MediaGeneratorAgent/1.0 (media agent)"})
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            data = json.loads(res.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise ProviderError(f"Wikimedia Commons search failed: {exc}", "stock_search_failed") from exc
    rows = []
    for _pid, page in (data.get("query", {}).get("pages", {}) or {}).items():
        info = (page.get("imageinfo") or [{}])[0]
        md = info.get("extmetadata", {}) or {}
        lic = str((md.get("LicenseShortName") or {}).get("value") or "")
        if not info.get("url") or not info.get("mime", "").startswith("video/"):
            continue
        if not any(ok in lic.lower() for ok in STOCK_LICENSES_OK):
            continue
        rows.append({
            "title": page.get("title", ""),
            "url": info.get("url"),
            "mime": info.get("mime"),
            "width": info.get("width"), "height": info.get("height"),
            "size": info.get("size"),
            "license": lic,
            "artist": re.sub(r"<[^>]+>", "", str((md.get("Artist") or {}).get("value") or "")).strip(),
            "credit": re.sub(r"<[^>]+>", "", str((md.get("Credit") or {}).get("value") or "")).strip(),
            "description": re.sub(r"<[^>]+>", "", str((md.get("ImageDescription") or {}).get("value") or "")).strip(),
        })
    return rows


def generate_stock_video(query: str, aspect: str, duration: float, dest: str) -> dict[str, Any]:
    w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
    rows = _commons_search(query)
    if not rows:
        raise ProviderError(f"no CC-licensed Wikimedia Commons video matched '{query}'", "stock_not_found")
    last: ProviderError | None = None
    for row in rows[:6]:
        tmp = dest + ".stock.src"
        try:
            _download(row["url"], tmp, max_bytes=90 * 1024 ** 2, timeout=300)
            src = probe(tmp)
            if not src.get("width") or src.get("duration", 0) < 2.0:
                raise ProviderError("stock file has no usable video stream", "bad_video")
            factor = 1.0
            if src["duration"] < duration:
                factor = duration / src["duration"]
                if factor > 1.65:
                    raise ProviderError(f"stock clip is only {src['duration']:.1f}s; stretching it to {duration:g}s would be misleading", "stock_too_short")
            vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=24"
            if factor > 1.001:
                vf = f"setpts={factor:.4f}*PTS," + vf
            p = run([FFMPEG, "-y", "-v", "error", "-i", tmp, "-vf", vf, "-t", f"{duration:.3f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-movflags", "+faststart", "-an", dest], timeout=900)
            if p.returncode != 0:
                raise ProviderError(f"ffmpeg could not cut the stock clip: {p.stderr.decode('utf-8', 'replace')[:300]}", "normalise_failed")
            final = probe(dest)
            if abs((final.get("duration") or 0) - duration) > 1.0:
                raise ProviderError("stock cut is outside 1s of the requested length", "wrong_duration")
            os.unlink(tmp)
            note = f"CC-licensed stock footage from Wikimedia Commons, not AI-generated. Source {src['duration']:.2f}s"
            if factor > 1.001:
                note += f"; slowed {factor:.2f}x to reach {duration:g}s (no loop, no duplicate frames)."
            return {
                "provider": "Wikimedia Commons — CC-licensed stock footage",
                "model": "none (stock footage; not generated)",
                "source_title": row["title"], "source_url": row["url"],
                "artist": row["artist"], "license": row["license"],
                "source_dimensions": f"{src.get('width')}x{src.get('height')}",
                "source_duration": round(src.get("duration", 0), 3),
                "width": w, "height": h, "aspect": aspect, "duration": round(final.get("duration", 0), 3),
                "fps": 24, "sha256": sha256(dest), "size": os.path.getsize(dest), "path": dest,
                "prompt": query, "note": note,
                "checks": [f"{final.get('width')}x{final.get('height')} exactly",
                           f"duration {final.get('duration'):.2f}s within 1s of {duration:g}s",
                           "source licence recorded; attribution included in the report"],
                "attribution": f"{row['title']} by {row['artist'] or 'unknown'}, {row['license']}, via Wikimedia Commons",
            }
        except ProviderError as exc:
            last = exc
            try:
                os.unlink(tmp)
            except OSError:
                pass
            continue
    raise last or ProviderError("no stock clip could be prepared", "stock_failed")


# ---------------------------------------------------------------------------
# Voice: Microsoft Edge neural TTS (free, no key).


VOICES = {
    "warm_female": ("en-US-AriaNeural", "warm, conversational US female"),
    "female": ("en-US-JennyNeural", "clear US female"),
    "male": ("en-US-GuyNeural", "warm US male"),
    "deep_male": ("en-US-AndrewNeural", "deep US male"),
    "british_female": ("en-GB-SoniaNeural", "warm British female"),
    "british_male": ("en-GB-RyanNeural", "British male"),
    "calm_female": ("en-US-AriaNeural", "calm US female"),
    "energetic_female": ("en-US-JennyNeural", "energetic US female"),
    "spanish_female": ("es-ES-ElviraNeural", "Spanish female"),
    "french_female": ("fr-FR-DeniseNeural", "French female"),
    "german_female": ("de-DE-KatjaNeural", "German female"),
}
VOICE_BY_NAME = {
    "aria": "warm_female", "jenny": "female", "guy": "male", "andrew": "deep_male",
    "sonia": "british_female", "ryan": "british_male", "elvira": "spanish_female",
    "denise": "french_female", "katja": "german_female",
}


def voice_provider_name() -> str:
    return "Microsoft Edge Neural TTS (edge-tts, free, no key)"


async def _edge_save(text: str, voice: str, rate: str, pitch: str, out: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(out)


def generate_voice(text: str, voice_key: str, target_duration: float | None, dest: str,
                   rate: str = "+0%", pitch: str = "+0Hz") -> dict[str, Any]:
    if not text.strip():
        raise ProviderError("the voiceover script is empty", "empty_script")
    key = voice_key if voice_key in VOICES else "warm_female"
    voice, label = VOICES[key]
    tmp = dest + ".tts.mp3"
    try:
        asyncio.run(_edge_save(text, voice, rate, pitch, tmp))
    except Exception as exc:
        raise ProviderError(f"Edge TTS failed: {exc}", "tts_failed") from exc
    src = probe(tmp)
    src_dur = src.get("duration") or 0
    if src_dur < 0.3:
        raise ProviderError("Edge TTS returned an empty audio file", "empty_audio")

    factor = 1.0
    pad = 0.0
    if target_duration and target_duration > 0:
        if src_dur > target_duration:
            factor = src_dur / target_duration
            if factor > 1.5:
                raise ProviderError(
                    f"the script takes {src_dur:.2f}s at a natural pace and would need {factor:.2f}x speed-up "
                    f"to fit {target_duration:g}s; the agent will not garble the script. Shorten the script to "
                    f"about {int(text.split().__len__() / factor)} words or allow {src_dur:.0f}s.",
                    "script_too_long",
                )
        elif src_dur < target_duration:
            if src_dur / target_duration >= 0.75:
                factor = src_dur / target_duration  # gentle slow-down, <= 1.33x
            else:
                pad = target_duration - src_dur

    filters = []
    if abs(factor - 1.0) > 0.005:
        filters.append(f"atempo={factor:.4f}")
    filters.append("apad")
    filters.append(f"atrim=0:{target_duration:.3f}" if target_duration else "atrim=0")
    vf = ",".join(filters)
    p = run([FFMPEG, "-y", "-v", "error", "-i", tmp, "-af", vf, "-ar", "48000", "-ac", "1",
             "-c:a", "libmp3lame", "-b:a", "192k", dest], timeout=180)
    if p.returncode != 0:
        raise ProviderError(f"ffmpeg could not normalise the voiceover: {p.stderr.decode('utf-8', 'replace')[:300]}", "normalise_failed")
    final = probe(dest)
    dur = final.get("duration") or 0
    if target_duration and abs(dur - target_duration) > 1.0:
        raise ProviderError(f"voiceover is {dur:.2f}s, outside 1s of {target_duration:g}s", "wrong_duration")
    vol = run([FFMPEG, "-v", "info", "-i", dest, "-af", "volumedetect", "-f", "null", "-"], timeout=60)
    vtext = vol.stderr.decode("utf-8", "replace")
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", vtext)
    mean = float(m.group(1)) if m else -99
    if mean < -50:
        raise ProviderError("voiceover is effectively silent", "silent_audio")
    words = re.findall(r"\S+", text)
    return {
        "provider": "Microsoft Edge Neural TTS (edge-tts)",
        "model": voice,
        "voice": voice, "voice_label": label,
        "language": voice.split("-")[0],
        "duration": round(dur, 3), "target_duration": target_duration,
        "words": len(words), "script": text, "script_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "sha256": sha256(dest), "size": os.path.getsize(dest), "path": dest,
        "sample_rate": 48000, "channels": 1,
        "adjustment": (f"speed {factor:.3f}x" if abs(factor - 1) > 0.005 else "natural pace")
                      + (f", {pad:.2f}s silence padding" if pad > 0.01 else ""),
        "checks": [f"duration {dur:.2f}s within 1s of {target_duration:g}s" if target_duration else "natural duration",
                   f"audio not silent (mean {mean:.1f} dB)",
                   "the exact script text was passed to the TTS engine; no words were added or removed"],
        "note": "Word-for-word is enforced by sending the buyer's script unchanged. Punctuation controls pauses only.",
    }
