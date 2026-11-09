"""End-to-end clipping pipeline and durable job state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import media
import planner


ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("VCA_STATE_DIR", os.path.join(ROOT, "state"))
WORK_DIR = os.environ.get("VCA_WORK_DIR", os.path.join(ROOT, "work"))
JOB_DIR = os.path.join(STATE_DIR, "jobs")
os.makedirs(JOB_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)

_state_lock = threading.RLock()


class PipelineError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def _atomic_write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def job_path(job_id: str) -> str:
    return os.path.join(JOB_DIR, f"{job_id}.json")


def save_job(state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = utcnow()
    with _state_lock:
        _atomic_write(job_path(state["job_id"]), json.dumps(state, indent=1, ensure_ascii=False))
    return state


def load_job(job_id: str) -> dict[str, Any] | None:
    path = job_path(job_id)
    if not os.path.exists(path):
        return None
    with _state_lock:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    rows = []
    for name in os.listdir(JOB_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOB_DIR, name), "r", encoding="utf-8") as fh:
                rows.append(json.load(fh))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


def update_job(job_id: str, **fields: Any) -> dict[str, Any]:
    with _state_lock:
        state = load_job(job_id)
        if state is None:
            raise PipelineError(f"unknown job {job_id}")
        state.update(fields)
        return save_job(state)


def _now_short() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _redact_url(url: str) -> str:
    if not url:
        return ""
    base, _, query = url.partition("?")
    return base + ("?…" if query else "")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _transcript_for(cand: planner.Candidate) -> str:
    words = [w for w in cand.words if w["end"] > cand.start + 0.1 and w["start"] < cand.end - 0.1]
    if not words:
        return ""
    text = " ".join(w["text"] for w in words)
    return re.sub(r"\s+", " ", text).strip()


def _captions_for(cand: planner.Candidate, fmt: str) -> list[dict[str, Any]]:
    words = [w for w in cand.words if w["end"] > cand.start + 0.05 and w["start"] < cand.end - 0.05]
    if not words:
        return []
    if fmt == "9:16":
        return media.group_words_into_cues(words, cand.start, cand.end, max_words=7, max_chars=40)
    if fmt == "1:1":
        return media.group_words_into_cues(words, cand.start, cand.end, max_words=8, max_chars=46)
    return media.group_words_into_cues(words, cand.start, cand.end, max_words=10, max_chars=62)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds/60:.1f} min"


def _public_base() -> str:
    try:
        with open(os.path.join(STATE_DIR, "tunnel.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        url = (data.get("url") or "").rstrip("/")
        if url:
            return url
    except Exception:
        pass
    return os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8788").rstrip("/")


def _source_label(state: dict[str, Any]) -> str:
    url = state.get("source_url") or ""
    if url:
        return _redact_url(url)
    name = state.get("source_filename") or "uploaded file"
    return name


def _check_duration(duration: float) -> None:
    if duration < 3:
        raise PipelineError("the source is shorter than 3 seconds, so there is nothing to clip")
    if duration > 3 * 3600:
        raise PipelineError(
            "the source is longer than 3 hours, which is outside this agent's current limit; "
            "split it into parts or ask for a custom job"
        )


def _download_attachment(
    attachments: list[dict[str, Any]], market: Any | None, work: str, log: Callable[[str], None]
) -> str | None:
    videos = [
        a
        for a in attachments
        if str(a.get("content_type", "")).startswith("video/")
        or re.search(r"\.(mp4|mov|webm|mkv|m4v|ogv)$", str(a.get("filename", "")), re.I)
    ]
    if not videos:
        return None
    att = videos[0]
    dest = os.path.join(work, "source-" + re.sub(r"[^A-Za-z0-9._-]", "-", att.get("filename", "upload.mp4")))
    if market is None:
        raise PipelineError("an uploaded file was provided, but this job has no marketplace credential to fetch it")
    log(f"downloading attachment {att.get('filename', 'upload')}")
    market.download(att.get("download_url") or att.get("url"), dest)
    expected = (att.get("sha256") or "").lower()
    if expected and _sha256(dest).lower() != expected:
        raise PipelineError("the uploaded file failed its SHA-256 integrity check")
    state_size = os.path.getsize(dest)
    if state_size < 1000:
        raise PipelineError("the uploaded file is empty")
    return dest


def _download_source(state: dict[str, Any], work: str, log: Callable[[str], None]) -> tuple[str, dict[str, Any], list[dict[str, float]]]:
    url = state.get("source_url")
    if not url:
        raise PipelineError("no video URL was provided")
    log("downloading the source video")
    max_height = int(os.environ.get("SOURCE_MAX_HEIGHT", "720"))
    path, info = media.download_source(url, work, max_height=max_height)
    heat = media.extract_heatmap(info, page_url=url if "youtube.com" in url or "youtu.be" in url else None)
    if not heat and ("youtube.com" in url or "youtu.be" in url):
        heat = media.extract_heatmap({}, page_url=url)
    return path, info, heat


def _render_all(
    state: dict[str, Any],
    analysis: planner.Analysis,
    candidates: list[planner.Candidate],
    brief: planner.Brief,
    source_path: str,
    work: str,
    on_progress: Callable[[int, str], None] | None,
) -> list[dict[str, Any]]:
    formats = brief.formats
    total_seconds = sum(max(0.2, c.end - c.start) for c in candidates) * len(formats)
    # Per-format budgets: split the 92 MiB envelope by output seconds.
    format_seconds = {fmt: sum(max(0.2, c.end - c.start) for c in candidates) for fmt in formats}
    resolution: dict[str, tuple[int, int, int]] = {}
    for fmt in formats:
        share = format_seconds[fmt] / max(1.0, total_seconds)
        resolution[fmt] = media.choose_resolution(fmt, format_seconds[fmt], int(92 * 1024**2 * share))

    clips: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates, 1):
        clip_dir = os.path.join(work, "clips", f"clip-{idx:02d}")
        os.makedirs(clip_dir, exist_ok=True)
        reason, metrics = planner.make_reason(analysis, cand.start, cand.end, _transcript_for(cand))
        clip_text = _transcript_for(cand)
        entry: dict[str, Any] = {
            "index": idx,
            "start": round(cand.start, 3),
            "end": round(cand.end, 3),
            "duration": round(cand.end - cand.start, 3),
            "reason": reason,
            "metrics": {k: round(v, 2) for k, v in metrics.items()},
            "transcript": clip_text,
            "formats": {},
            "captions": {"requested": brief.captions, "status": "not requested"},
        }
        if brief.captions:
            if not cand.words or len(clip_text) < 4:
                entry["captions"] = {
                    "requested": True,
                    "status": "no speech detected",
                    "detail": "Whisper found no intelligible speech in this window; no caption file was burned in.",
                }
            else:
                entry["captions"] = {"requested": True, "status": "burned in"}

        for fmt in formats:
            width, height, maxrate = resolution[fmt]
            out = os.path.join(clip_dir, f"clip-{idx:02d}_{fmt.replace(':','x')}.mp4")
            ass_path = None
            cues: list[dict[str, Any]] = []
            if brief.captions and entry["captions"]["status"] == "burned in":
                cues = _captions_for(cand, fmt)
                if cues:
                    ass_path = os.path.join(clip_dir, f"clip-{idx:02d}_{fmt.replace(':','x')}.ass")
                    media.make_ass(cues, cand.start, fmt, ass_path)
            if on_progress:
                pct = 55 + int(30 * ((idx - 1) * len(formats) + formats.index(fmt)) / max(1, len(candidates) * len(formats)))
                on_progress(pct, f"rendering clip {idx} of {len(candidates)} in {fmt}")
            try:
                info = media.render_clip(
                    source_path,
                    cand.start,
                    cand.end,
                    fmt,
                    out,
                    width,
                    height,
                    maxrate,
                    ass_path=ass_path,
                    has_audio=analysis.speech_intervals != [] or analysis.energy != [],
                )
                entry["formats"][fmt] = {
                    "path": out,
                    "width": info["width"],
                    "height": info["height"],
                    "duration": info["duration"],
                    "size": info["size"],
                    "maxrate": maxrate,
                    "ass": ass_path,
                    "cues": len(cues),
                }
                entry["captions"]["cues"] = max(entry["captions"].get("cues", 0), len(cues))
            except Exception as exc:
                entry["formats"][fmt] = {
                    "path": out,
                    "error": str(exc)[:800],
                    "width": 0,
                    "height": 0,
                    "duration": 0,
                    "size": 0,
                }
        clips.append(entry)
    return clips


def _total_size(clips: list[dict[str, Any]]) -> int:
    total = 0
    for c in clips:
        for f in c.get("formats", {}).values():
            total += int(f.get("size") or 0)
    return total


def _shrink_outputs(
    clips: list[dict[str, Any]],
    source_path: str,
    analysis: planner.Analysis,
    work: str,
    on_progress: Callable[[int, str], None] | None,
) -> None:
    """If the encodes overrun the 100 MiB submit limit, lower the bitrate."""
    limit = 94 * 1024**2
    if _total_size(clips) <= limit:
        return
    for attempt in range(3):
        if _total_size(clips) <= limit:
            return
        if on_progress:
            on_progress(80 + attempt * 2, "output is above the upload budget; re-encoding smaller")
        factor = 0.62 if attempt == 0 else 0.75
        for c in clips:
            for fmt, info in list(c["formats"].items()):
                if info.get("error") or not info.get("path") or not os.path.exists(info["path"]):
                    continue
                width, height = info["width"], info["height"]
                maxrate = max(550_000, int(info.get("maxrate", 2_000_000) * factor))
                tmp = info["path"] + ".small.mp4"
                try:
                    new = media.render_clip(
                        source_path,
                        c["start"],
                        c["end"],
                        fmt,
                        tmp,
                        width,
                        height,
                        maxrate,
                        ass_path=info.get("ass"),
                        has_audio=analysis.speech_intervals != [],
                    )
                    os.replace(tmp, info["path"])
                    info.update({"size": new["size"], "maxrate": maxrate, "width": new["width"], "height": new["height"]})
                except Exception:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                if _total_size(clips) <= limit:
                    return


def _build_report(
    state: dict[str, Any],
    brief: planner.Brief,
    source_duration: float,
    has_heat: bool,
    clips: list[dict[str, Any]],
    retention_label: str,
    revision_note: str = "",
    file_urls: dict[str, str] | None = None,
) -> str:
    file_urls = file_urls or {}
    lines: list[str] = []
    lines.append(f"# Video clips — {state.get('title') or 'your video'}")
    lines.append("")
    lines.append(f"**Source:** {_source_label(state)}  ")
    lines.append(f"**Source length:** {planner.fmt_time(source_duration)}  ")
    lines.append(f"**Delivered:** {len(clips)} clip(s) in {', '.join(brief.formats)}  ")
    if len(clips) < brief.clip_count:
        lines.append(
            f"**Count note:** you asked for {brief.clip_count} clips; the source length and the non-overlap rule "
            f"allowed {len(clips)} clean clip(s). I did not duplicate or overlap moments to hit the number."
        )
        lines.append("")
    lines.append(f"**Moment selection:** {retention_label}  ")
    if revision_note:
        lines.append(f"**Revision:** {revision_note}  ")
    lines.append("")
    lines.append("## What you asked for")
    lines.append("")
    lines.append("| Item | Value |")
    lines.append("|---|---|")
    lines.append(f"| Clips | {brief.clip_count} |")
    lines.append(f"| Formats | {', '.join(brief.formats)} |")
    lines.append(f"| Captions | {'yes' if brief.captions else 'no'} |")
    lines.append(f"| Source | {_source_label(state)} |")
    lines.append("")
    lines.append("## Clips")
    lines.append("")
    for c in clips:
        lines.append(f"### Clip {c['index']} — {planner.fmt_time(c['start'])}–{planner.fmt_time(c['end'])} ({c['duration']:.0f}s)")
        lines.append("")
        lines.append(f"**Why this moment:** {c['reason']}")
        lines.append("")
        if brief.target_max and c["duration"] > brief.target_max * 1.15:
            lines.append(
                f"**Length note:** you asked for up to {brief.target_max:.0f}s; the nearest complete-sentence boundary made this "
                f"{c['duration']:.0f}s. Cutting at {brief.target_max:.0f}s would have landed mid-sentence."
            )
            lines.append("")
        elif brief.target_min and c["duration"] < brief.target_min * 0.85:
            lines.append(
                f"**Length note:** you asked for at least {brief.target_min:.0f}s; the nearest complete-sentence boundary made this "
                f"{c['duration']:.0f}s. Extending further would have included a new, unrelated sentence."
            )
            lines.append("")
        if c.get("transcript"):
            lines.append(f"**Opening words:** “{planner.short_quote(c['transcript'], 220)}”")
            lines.append("")
        files = []
        for fmt, info in c.get("formats", {}).items():
            if info.get("error"):
                files.append(f"{planner.FORMAT_LABELS.get(fmt, fmt)}: not produced — {info['error']}")
            else:
                key = f"{c['index']}:{fmt}"
                url = file_urls.get(key) or file_urls.get(info.get("path", ""))
                label = os.path.basename(info.get("path", "clip.mp4"))
                if url:
                    files.append(f"[{label}]({url}) — {info['width']}×{info['height']}, {info['size']/1024/1024:.1f} MB")
                else:
                    files.append(f"{label} — {info['width']}×{info['height']}, {info['size']/1024/1024:.1f} MB")
        lines.append("**Files:** " + "; ".join(files))
        lines.append("")
        if c.get("captions", {}).get("requested") or brief.captions:
            cap = c.get("captions", {})
            if cap.get("status") == "burned in":
                lines.append(f"**Captions:** burned in, {cap.get('cues', 0)} cue(s), large phone-readable text.")
            elif cap.get("status") == "no speech detected":
                lines.append("**Captions:** no intelligible speech was detected in this clip, so there was nothing to caption.")
            else:
                lines.append("**Captions:** not requested for this clip.")
            lines.append("")
    lines.append("## How the moments were chosen")
    lines.append("")
    if has_heat:
        lines.append(
            f"{retention_label}. The heatmap is the public 'most replayed' signal; each clip above states the peak it covers. "
            "No private analytics were used."
        )
    else:
        lines.append(
            "The source had no retention or 'most replayed' data available to this agent. Selection used content analysis: "
            "audio RMS energy, scene-change density, frame-to-frame motion, and speech density, with a minimum clip length "
            "and a penalty for overlapping windows. Every reason above reports the measured signals for that window."
        )
    lines.append("")
    lines.append("**Cut boundaries:** clip starts and ends are snapped to complete sentence boundaries when one is within reach, "
                 "then to complete-word and silence boundaries. When speech is present, a local Whisper pass provides word "
                 "timestamps, and a final check rejects a cut that lands inside a word. No clip is intentionally cut in the "
                 "middle of a word or sentence.")
    lines.append("")
    lines.append("## Technical")
    lines.append("")
    lines.append("| Clip | Format | Resolution | Duration | Size |")
    lines.append("|---|---|---|---|---|")
    for c in clips:
        for fmt, info in c.get("formats", {}).items():
            if info.get("error"):
                lines.append(f"| {c['index']} | {fmt} | failed | — | — |")
            else:
                lines.append(
                    f"| {c['index']} | {fmt} | {info['width']}×{info['height']} | {info['duration']:.1f}s | {info['size']/1024/1024:.1f} MB |"
                )
    lines.append("")
    lines.append("All clips are H.264/AAC MP4 with fast-start, 30 fps, and loudness normalisation to about −16 LUFS. "
                 "Captions, when requested, are burned in and sized for phone viewing.")
    lines.append("")
    lines.append("## Rights and retention")
    lines.append("")
    lines.append(
        "You confirmed that you have the rights to the source video and to any music or third-party material in it. "
        "This agent does not bypass DRM, private-video restrictions, or platform terms."
    )
    if brief.retention_mode == "none":
        lines.append(
            "**No-retention selection:** the source and intermediate files are deleted immediately after delivery. "
            "Because the source is gone, a later revision needs the video re-uploaded."
        )
    else:
        lines.append(
            "**Retention:** the source and working files are deleted within 24 hours of delivery. Delivered clips live in "
            "your Agent Market job result. Ask for the no-retention option to delete the source immediately after delivery."
        )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- Highlight quality is partly subjective. Retention data is only available on some public YouTube videos; "
        "elsewhere this is content analysis, which is a weaker signal."
    )
    lines.append(
        "- The agent does not understand jokes, irony, or off-screen context. It finds strong audio/visual/speech moments, "
        "not semantic punchlines."
    )
    lines.append(
        "- Caption accuracy depends on audio quality, accents, overlapping speech, and music. The local Whisper model is good "
        "on clear speech but can miss proper nouns. A transcript is included above so you can check it."
    )
    lines.append(
        "- Private, subscriber-only, DRM-protected, or geo-blocked videos cannot be downloaded. Send the file directly instead."
    )
    lines.append(
        "- Very long sources (over 3 hours) are outside the current limit."
    )
    lines.append("")
    return "\n".join(lines)


def run_pipeline(
    state: dict[str, Any],
    market: Any | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one clipping job. Returns the updated state. Raises PipelineError."""
    log = log or (lambda _m: None)

    def progress(pct: int, msg: str) -> None:
        state["progress"] = max(0, min(100, int(pct)))
        state["stage"] = msg
        state["progress_message"] = msg
        save_job(state)
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass
        log(msg)

    job_id = state["job_id"]
    work = os.path.join(WORK_DIR, job_id)
    os.makedirs(work, exist_ok=True)
    with _state_lock:
        state["status"] = "running"
        state["started_at"] = state.get("started_at") or utcnow()
        state["error"] = None
        save_job(state)

    try:
        progress(2, "reading the brief")
        brief = planner.parse_brief(state.get("brief", ""), has_attachment=bool(state.get("attachments")))
        state["brief_parsed"] = {
            "clip_count": brief.clip_count,
            "formats": brief.formats,
            "captions": brief.captions,
            "language": brief.language,
            "retention_mode": brief.retention_mode,
            "video_url": _redact_url(brief.video_url or ""),
            "errors": brief.errors,
        }
        if brief.errors and not state.get("attachments") and not state.get("source_path") and not state.get("source_url"):
            raise PipelineError(brief.errors[0])
        if brief.errors and (state.get("attachments") or state.get("source_path") or state.get("source_url")):
            # An attachment or an already-known source URL satisfies the input requirement.
            brief.errors = [e for e in brief.errors if "video link" not in e]
        if not brief.video_url and state.get("source_url"):
            brief.video_url = state["source_url"]
        if not state.get("source_url") and brief.video_url:
            state["source_url"] = brief.video_url
        save_job(state)

        # Source
        source_path = state.get("source_path")
        info: dict[str, Any] = {}
        heat: list[dict[str, float]] = []
        if state.get("attachments"):
            if not source_path or not os.path.exists(source_path):
                source_path = _download_attachment(state["attachments"], market, work, log)
            if not source_path:
                raise PipelineError("the brief has an attachment, but none of them is a video file")
            state["source_path"] = source_path
            state["source_filename"] = os.path.basename(source_path)
        if not source_path:
            progress(8, "downloading the source video")
            source_path, info, heat = _download_source(state, work, log)
            state["source_path"] = source_path
            state["source_url"] = state.get("source_url") or brief.video_url
            state["source_filename"] = os.path.basename(source_path)
            if info.get("title"):
                state["title"] = str(info.get("title"))[:200]
        save_job(state)

        progress(12, "probing the source")
        src = media.probe(source_path)
        _check_duration(src["duration"])
        state["source_info"] = {
            "duration": round(src["duration"], 2),
            "width": src["width"],
            "height": src["height"],
            "fps": round(src["fps"], 2),
            "has_audio": src["has_audio"],
            "size": src["size"],
        }
        state["source_sha256"] = _sha256(source_path)
        save_job(state)

        if not heat and state.get("source_url"):
            heat = media.extract_heatmap({}, page_url=state["source_url"])
        if heat:
            heat_source = f"YouTube public most-replayed heatmap ({len(heat)} markers)"
        else:
            heat_source = None

        progress(18, "analysing audio energy, scene changes, motion and speech")
        analysis = planner.build_analysis(
            source_path,
            src["duration"],
            src["has_audio"],
            heat=heat,
            heat_source=heat_source,
        )
        state["analysis"] = {
            "duration": round(analysis.duration, 2),
            "scene_cuts": len(analysis.scenes),
            "silences": len(analysis.silences),
            "heat_markers": len(analysis.heat),
            "heat_source": heat_source,
            "energy_bins": len(analysis.energy),
            "motion_bins": len(analysis.motion),
        }
        # Keep compact feature series so a targeted revision can recompute
        # reasons without re-analysing the whole source.
        state["analysis_features"] = {
            "duration": round(analysis.duration, 2),
            "energy_pct": [round(x, 4) for x in analysis.energy_pct],
            "motion_pct": [round(x, 4) for x in analysis.motion_pct],
            "speech_fraction": [round(x, 4) for x in analysis.speech_fraction],
            "scene_count": [round(x, 3) for x in analysis.scene_count],
            "score": [round(x, 4) for x in analysis.score],
            "heat": analysis.heat,
        }
        save_job(state)

        progress(25, "picking the strongest non-overlapping moments")
        candidates = planner.select_candidates(analysis, brief)
        if not candidates:
            raise PipelineError("the analysis found no usable clip windows in this source")
        state["candidates_raw"] = [
            {"start": round(c.start, 2), "end": round(c.end, 2), "score": round(c.score, 3)} for c in candidates
        ]
        save_job(state)

        if src["has_audio"] and not brief.language:
            progress(28, "detecting the spoken language")
            detected = media.detect_language(source_path, work)
            if detected:
                brief.language = detected
                state["brief_parsed"]["language"] = detected
                save_job(state)
                log(f"detected language: {detected}")

        progress(30, "finding clean sentence and word boundaries")
        refined: list[planner.Candidate] = []
        for i, cand in enumerate(candidates, 1):
            try:
                refined.append(
                    planner.refine_boundaries(
                        cand,
                        analysis,
                        brief,
                        source_path,
                        work,
                        transcribe=src["has_audio"],
                    )
                )
            except Exception as exc:
                log(f"boundary transcription failed for clip {i}: {exc}")
                cand.reason = ""
                refined.append(cand)
            if on_progress:
                on_progress(30 + int(15 * i / len(candidates)), f"checked boundaries for clip {i}")
        candidates = refined
        candidates = planner._remove_overlaps(candidates, analysis)
        state["candidates"] = [
            {
                "start": round(c.start, 2),
                "end": round(c.end, 2),
                "duration": round(c.end - c.start, 2),
                "raw_start": round(c.raw_start, 2),
                "raw_end": round(c.raw_end, 2),
                "score": round(c.score, 3),
            }
            for c in candidates
        ]
        save_job(state)

        progress(50, "rendering each clip for every requested format")
        clips = _render_all(state, analysis, candidates, brief, source_path, work, on_progress)
        _shrink_outputs(clips, source_path, analysis, work, on_progress)
        state["clips"] = clips
        state["total_size"] = _total_size(clips)
        save_job(state)

        failed_formats = [
            (c["index"], fmt, info.get("error", "unknown"))
            for c in clips
            for fmt, info in c.get("formats", {}).items()
            if info.get("error")
        ]
        if len(failed_formats) == len(clips) * len(brief.formats):
            raise PipelineError("every requested format failed to render; the source may be unreadable")
        if failed_formats:
            log(f"{len(failed_formats)} format(s) failed; they will be named in the report")

        retention_label = heat_source or "content analysis (no retention or 'most replayed' data was available)"
        report = _build_report(
            state,
            brief,
            src["duration"],
            bool(heat),
            clips,
            retention_label,
        )
        state["report_md"] = report
        state["status"] = "rendered"
        state["progress"] = 85
        state["stage"] = "rendered; uploading deliverables"
        save_job(state)
        log("rendering finished")
        return state
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        state["failed_at"] = utcnow()
        state["traceback"] = traceback.format_exc()[-5000:]
        save_job(state)
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(str(exc)) from exc


def upload_outputs(
    state: dict[str, Any],
    market: Any,
    on_progress: Callable[[int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Upload every rendered MP4 to Agent Market; return key -> public URL."""
    log = log or (lambda _m: None)
    urls: dict[str, str] = {}
    clips = state.get("clips", [])
    total = sum(1 for c in clips for _ in c.get("formats", {}))
    done = 0
    for c in clips:
        for fmt, info in c.get("formats", {}).items():
            if info.get("error") or not info.get("path"):
                continue
            done += 1
            if info.get("file_id") and info.get("file_url"):
                urls[f"{c['index']}:{fmt}"] = info["file_url"]
                continue
            if on_progress:
                on_progress(85 + int(10 * done / max(1, total)), f"uploading clip {c['index']} ({fmt})")
            log(f"uploading clip {c['index']} {fmt}")
            try:
                up = market.upload_file(info["path"], "video/mp4")
                info["file_id"] = up.get("fileId")
                info["file_url"] = up.get("fileUrl")
                info["sha256"] = up.get("sha256")
                if info.get("file_url"):
                    urls[f"{c['index']}:{fmt}"] = info["file_url"]
            except Exception as exc:
                info["upload_error"] = str(exc)[:800]
                log(f"upload failed for clip {c['index']} {fmt}: {exc}")
                # Keep a local public URL as a last resort for direct jobs.
                base = _public_base()
                urls[f"{c['index']}:{fmt}"] = f"{base}/v1/jobs/{state['job_id']}/files/{os.path.basename(info['path'])}"
    save_job(state)
    return urls


def submit_deliverable(
    state: dict[str, Any],
    market: Any,
    assignment_id: str,
    file_urls: dict[str, str],
    on_progress: Callable[[int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    log = log or (lambda _m: None)
    clips = state.get("clips", [])
    brief = planner.parse_brief(state.get("brief", ""), has_attachment=bool(state.get("attachments")))
    retention_label = (state.get("analysis") or {}).get("heat_source") or "content analysis (no retention data available)"
    report = _build_report(
        state,
        brief,
        (state.get("source_info") or {}).get("duration", 0.0),
        bool((state.get("analysis") or {}).get("heat_markers")),
        clips,
        retention_label,
        revision_note=state.get("revision_note", ""),
        file_urls=file_urls,
    )
    state["report_md"] = report
    files: list[str] = []
    for c in clips:
        for fmt, info in c.get("formats", {}).items():
            if info.get("file_id"):
                files.append(info["file_id"])
            elif info.get("file_url"):
                files.append(info["file_url"])
    if on_progress:
        on_progress(97, "submitting the clips and the report")
    log(f"submitting deliverable for {assignment_id} ({len(files)} files)")
    result = market.submit(assignment_id, report, files=files)
    state.setdefault("submissions", []).append(
        {"assignment_id": assignment_id, "submitted_at": utcnow(), "files": files, "result": result if isinstance(result, dict) else {}}
    )
    state["status"] = "submitted"
    state["progress"] = 100
    state["stage"] = "submitted"
    save_job(state)
    return result


def purge_source(state: dict[str, Any], reason: str = "retention policy") -> None:
    source = state.get("source_path")
    work = os.path.join(WORK_DIR, state["job_id"])
    if source and os.path.exists(source) and work in os.path.abspath(source):
        try:
            os.remove(source)
        except Exception:
            pass
    # remove intermediate wavs/analysis frames but keep clips for direct serving
    for name in ("asr", "tmp"):
        path = os.path.join(work, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    state["source_purged_at"] = utcnow()
    state["source_purged_reason"] = reason
    save_job(state)


def cleanup_old_work(max_age_hours: float = 24.0) -> int:
    """Delete source/intermediate folders for finished jobs older than the window."""
    removed = 0
    for state in list_jobs(limit=500):
        if state.get("status") in ("queued", "running", "rendered"):
            continue
        stamp = state.get("updated_at") or state.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            age = time.time() - dt.timestamp()
        except Exception:
            age = 0
        if age < max_age_hours * 3600:
            continue
        work = os.path.join(WORK_DIR, state["job_id"])
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
            state["work_purged_at"] = utcnow()
            state["work_purged_reason"] = f"older than {max_age_hours:g}h"
            save_job(state)
            removed += 1
    return removed
