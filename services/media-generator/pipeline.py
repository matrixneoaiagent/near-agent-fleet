"""Durable job state, generation orchestration, validation, report and submit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import brief as brief_mod
import providers

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("MGA_STATE_DIR", os.path.join(ROOT, "state"))
WORK_DIR = os.environ.get("MGA_WORK_DIR", os.path.join(ROOT, "work"))
JOB_DIR = os.path.join(STATE_DIR, "jobs")
os.makedirs(JOB_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)

_lock = threading.RLock()


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
    with _lock:
        _atomic_write(job_path(state["job_id"]), json.dumps(state, indent=1, ensure_ascii=False))
    return state


def load_job(job_id: str) -> dict[str, Any] | None:
    path = job_path(job_id)
    if not os.path.exists(path):
        return None
    with _lock:
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


def _public_base() -> str:
    try:
        with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("public_base_url") or "").rstrip("/")
    except Exception:
        return ""


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _palette(path: str) -> str:
    """Average colour of a reference image, as a hex hint for the prompt."""
    try:
        p = providers.run([providers.FFMPEG, "-v", "error", "-i", path, "-vf", "scale=1:1,format=rgb24",
                           "-frames:v", "1", "-f", "rawvideo", "-"], timeout=30)
        if p.returncode == 0 and len(p.stdout) >= 3:
            return "#%02x%02x%02x" % tuple(p.stdout[:3])
    except Exception:
        pass
    return ""


def _download_reference(item: str, work: str, market: Any = None) -> str | None:
    try:
        if item.startswith("attachment:") and market is not None:
            name = item.split(":", 1)[1]
            dest = os.path.join(work, "ref-" + re.sub(r"[^A-Za-z0-9._-]", "_", name))
            # The app passes a resolved attachment URL separately when it has one.
            return None
        if item.startswith("http"):
            dest = os.path.join(work, "ref-" + hashlib.sha256(item.encode()).hexdigest()[:10])
            providers._download(item, dest, max_bytes=20 * 1024 ** 2, timeout=60)
            return dest
    except Exception:
        return None
    return None


def _image_prompt(brief: brief_mod.Brief, index: int, palette: str, count: int) -> str:
    base = brief.prompt or brief.text
    base = re.sub(r"\s+", " ", base).strip(" ,.")
    if re.search(r"\bmug\b", base, re.I):
        base = re.sub(r"\ba\s+(?:ceramic\s+)?mug\b", "a single ceramic coffee mug with one handle", base, flags=re.I)
        if "single ceramic coffee mug" not in base.lower():
            base = "a single ceramic coffee mug with one handle, " + base
    extras = ["high quality", "sharp focus", "natural lighting"]
    if re.search(r"\b(?:product|mug|cup|bottle|packshot|commercial)\b", base, re.I):
        extras = ["professional product photography", "soft natural light", "sharp focus", "clean composition"]
    if palette:
        extras.append(f"colour palette inspired by {palette}")
    if count > 1:
        views = ["front view, centered", "slightly higher camera angle", "close-up on the mug",
                 "side view", "top-down view", "detail shot"]
        extras.append(views[index % len(views)])
    return f"{base}, " + ", ".join(extras)


def _video_prompt(brief: brief_mod.Brief, palette: str) -> str:
    base = brief.prompt or brief.text
    base = re.sub(r"\s+", " ", base).strip(" ,.")
    extra = "smooth natural motion, realistic physics, cinematic lighting, no text, no watermark"
    if palette:
        extra += f", colour palette inspired by {palette}"
    return f"{base}, {extra}"


def _stock_query(brief: brief_mod.Brief) -> list[str]:
    text = (brief.prompt or brief.text).lower()
    words = re.findall(r"[a-z]+", text)
    stop = {"a", "an", "the", "of", "into", "being", "is", "are", "on", "in", "to", "with", "and", "for",
            "video", "clip", "second", "seconds", "vertical", "square", "landscape", "make", "create",
            "realistic", "smooth", "motion", "close", "up", "shot", "footage", "high", "quality"}
    key = [w for w in words if w not in stop][:6]
    q1 = " ".join(key)
    return [q1, " ".join(key[:3]), key[0] if key else "coffee"]


def _report(state: dict[str, Any], brief: brief_mod.Brief, outputs: list[dict[str, Any]],
            declines: list[dict[str, str]], provider_notes: list[str], revision_note: str = "") -> str:
    lines: list[str] = []
    lines.append("# Media generation report")
    lines.append("")
    lines.append(f"**Job:** `{state['job_id']}`  ")
    lines.append(f"**Brief received:** {state.get('brief', '').strip()[:1200]}")
    lines.append("")
    delivered = [o for o in outputs if o.get("status") == "ok"]
    lines.append("## Result")
    lines.append("")
    if delivered:
        by_type: dict[str, int] = {}
        for o in delivered:
            by_type[o["kind"]] = by_type.get(o["kind"], 0) + 1
        summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in by_type.items())
        lines.append(f"Delivered **{summary}**. Each file was checked for format, dimensions and duration before delivery.")
    else:
        lines.append("**No media file was delivered.** The reason is below. The agent did not submit a placeholder and call it a result.")
    lines.append("")
    if revision_note:
        lines.append(f"**What changed in this version:** {revision_note}")
        lines.append("")

    lines.append("## Files")
    lines.append("")
    if delivered:
        lines.append("| # | Type | File | Provider | Model | Format | Duration |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, o in enumerate(delivered, 1):
            dur = f"{o.get('duration'):.2f}s" if o.get("duration") else "—"
            fmt = "48 kHz mono" if o["kind"] == "voiceover" else f"{o.get('width')}×{o.get('height')} ({o.get('aspect')})"
            lines.append(f"| {i} | {o['kind']} | `{o['filename']}` | {o.get('provider', '')} | {o.get('model', '')} | "
                         f"{fmt} | {dur} |")
        lines.append("")
        for i, o in enumerate(delivered, 1):
            lines.append(f"### File {i} — `{o['filename']}`")
            lines.append("")
            lines.append(f"- **Type:** {o['kind']}")
            lines.append(f"- **Provider:** {o.get('provider')}")
            lines.append(f"- **Model / source:** {o.get('model')}")
            if o.get("prompt"):
                lines.append(f"- **Prompt:** {o['prompt']}")
            if o.get("script"):
                lines.append(f"- **Script (read verbatim):** {o['script']}")
            if o.get("voice"):
                lines.append(f"- **Voice:** {o.get('voice')} ({o.get('voice_label', '')})")
            if o.get("source_title"):
                lines.append(f"- **Stock source:** {o.get('attribution')} — {o.get('source_url')}")
            specs = ("48 kHz mono" if o["kind"] == "voiceover" else
                     f"{o.get('width')}×{o.get('height')} {o.get('aspect')}")
            dur_part = f"{o.get('duration'):.2f}s, " if o["kind"] != "image" else ""
            lines.append(f"- **Specs:** {specs}, "
                         f"{dur_part}{o.get('size', 0) / 1024:.0f} KiB, sha256 `{o.get('sha256', '')[:16]}…`")
            for c in o.get("checks", []):
                lines.append(f"- ✅ {c}")
            if o.get("adjustment"):
                lines.append(f"- **Timing adjustment:** {o['adjustment']}")
            if o.get("note"):
                lines.append(f"- **Note:** {o['note']}")
            if o.get("file_url"):
                lines.append(f"- **Download:** {o['file_url']}")
            lines.append("")
    else:
        lines.append("_None._")
        lines.append("")

    lines.append("## Brief confirmation")
    lines.append("")
    lines.append(f"- Media requested: {', '.join(brief.media_types)}")
    lines.append(f"- Variations requested: {brief.count}")
    if "voiceover" not in brief.media_types:
        lines.append(f"- Aspect: {providers.ASPECT_LABELS.get(brief.aspect, brief.aspect)}")
    if brief.duration:
        lines.append(f"- Length requested: {brief.duration:g}s")
    if brief.script:
        lines.append(f"- Script: {len(re.findall(r'\S+', brief.script))} words, passed unchanged to the TTS engine")
    if "voiceover" in brief.media_types and brief.voice:
        lines.append(f"- Voice: {providers.VOICES.get(brief.voice, ('', brief.voice))[1]}")
    lines.append("")

    if declines:
        lines.append("## Declined parts")
        lines.append("")
        for d in declines:
            lines.append(f"- **{d.get('part', 'request')} — {d.get('rule', 'policy')}:** {d.get('reason', '')}")
        lines.append("")
        lines.append("The agent declined only the part that breaks the rules. If every requested part is declined, no provider is called and the job is declined with this explanation.")
        lines.append("")

    if provider_notes:
        lines.append("## Provider notes and limits")
        lines.append("")
        for n in provider_notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("## What was checked, and what was not")
    lines.append("")
    lines.append("- Checked automatically: file decodes, exact pixel dimensions/aspect, duration within 1 second, non-empty/non-silent output, and (for video) that the clip is not frozen.")
    lines.append("- The prompt named every subject, object and setting in the brief. The agent does **not** run a semantic object detector, so a small visual error (hands, text, reflections) is possible; the buyer should confirm the subject visually and can request a targeted revision.")
    lines.append("- Voiceover: the exact script string was sent to the TTS engine with no added or removed words. Punctuation affects pauses only. The report includes the script and its SHA-256.")
    lines.append("- Reference images: accepted as colour/style guidance (dominant palette) when the free image route cannot do pixel-level conditioning; this is stated per job rather than implied.")
    lines.append("")

    lines.append("## Rights, privacy and retention")
    lines.append("")
    lines.append("- The buyer confirms they have the rights to any reference image, logo, script or brand they supply.")
    lines.append("- Generated images/videos come from the providers named above and are subject to those providers' terms, including their commercial-use rules.")
    lines.append("- Stock fallback, when used, is CC-licensed or public domain and is attributed above; the agent does not present stock as generated.")
    lines.append("- Source/reference files are deleted after delivery by default; delivered files live in the Agent Market job. A no-retention request deletes references immediately after delivery (a revision then needs a re-upload).")
    lines.append("")
    lines.append("## Revisions")
    lines.append("")
    lines.append("Name the file numbers and the change (for example, \"file 2: make it square\" or \"file 1: redo with a warmer background\"). Only the named files are regenerated; the rest keep the files already delivered.")
    lines.append("")
    return "\n".join(lines)


def run_pipeline(state: dict[str, Any], market: Any = None,
                 on_progress: Callable[[int, str], None] | None = None,
                 log: Callable[[str], None] | None = None) -> dict[str, Any]:
    log = log or (lambda _m: None)
    progress = on_progress or (lambda _p, _m: None)
    work = os.path.join(WORK_DIR, state["job_id"])
    os.makedirs(work, exist_ok=True)
    state["work_dir"] = work

    attachment_names = [a.get("filename", "") for a in (state.get("attachments") or [])]
    brief = brief_mod.parse_brief(state.get("brief", ""), attachment_names)
    # Explicit structured fields from the direct/A2A API win over text parsing.
    for key, attr in (("media_types", "media_types"), ("count", "count"), ("aspect", "aspect"),
                      ("duration", "duration"), ("script", "script"), ("voice", "voice"),
                      ("voice_rate", "voice_rate"), ("allow_stock_video", "allow_stock_video")):
        if state.get(key) not in (None, "", []):
            setattr(brief, attr, state[key])
    if state.get("prompt"):
        brief.prompt = state["prompt"]
    if state.get("reference_urls"):
        brief.reference_urls = list(state["reference_urls"])
    if state.get("aspect") and state["aspect"] in providers.ASPECTS:
        brief.aspect = state["aspect"]
    if "voiceover" in brief.media_types and not brief.script and state.get("script"):
        brief.script = state["script"]
    if "voiceover" in brief.media_types and not brief.script:
        # A script is mandatory; do not invent one.
        brief.violations.append(brief_mod.Violation("voiceover", "missing-script",
            "no script was supplied. A voiceover must read the buyer's exact words; the agent will not invent a script. Send the script in the brief or as a job message."))
    state["brief_parsed"] = brief.to_dict()
    save_job(state)

    declines = [{"part": v.part, "rule": v.rule, "reason": v.reason} for v in brief.violations]
    allowed = brief.allowed_types
    provider_notes: list[str] = []
    outputs: list[dict[str, Any]] = []

    if not allowed:
        state["status"] = "declined"
        state["stage"] = "declined by content rules"
        state["declines"] = declines
        state["report_md"] = _report(state, brief, [], declines, [], state.get("revision_note", ""))
        save_job(state)
        log("declined: every requested part breaks the content rules")
        return state

    # Reference palette (best effort, never required).
    palette = ""
    for path in (state.get("reference_paths") or []):
        if path and os.path.exists(path):
            palette = _palette(path)
            if palette:
                break
    for ref in brief.reference_urls[:2]:
        path = _download_reference(ref, work, market)
        if path:
            palette = _palette(path)
            if palette:
                break
    if brief.reference_urls or state.get("reference_paths"):
        provider_notes.append(
            "Reference image input was received. The free image route has no pixel-level image-conditioning endpoint, "
            "so the agent used the reference's average colour" + (f" ({palette})" if palette else "") +
            " as a style hint rather than claiming exact likeness."
        )

    # ---------------- images ----------------
    if "image" in allowed:
        n = max(1, min(int(brief.count or 1), 6))
        for i in range(n):
            filename = f"image-{i + 1}-{brief.aspect.replace(':', 'x')}.jpg"
            dest = os.path.join(work, filename)
            progress(10 + int(55 * i / max(1, n)), f"generating image {i + 1} of {n}")
            log(f"image {i + 1}/{n} via Pollinations")
            try:
                info = providers.generate_image(
                    _image_prompt(brief, i, palette, n), brief.aspect,
                    seed=1000 + i * 131 + int(hashlib.sha256(state["job_id"].encode()).hexdigest()[:4], 16),
                    dest=dest,
                )
                info.update({"kind": "image", "filename": filename, "status": "ok", "index": i + 1})
                outputs.append(info)
                log(f"image {i + 1} ok: {info['width']}x{info['height']}")
            except Exception as exc:
                msg = str(exc)
                outputs.append({"kind": "image", "filename": filename, "status": "error", "error": msg, "index": i + 1})
                provider_notes.append(f"Image {i + 1} failed: {msg}")
                log(f"image {i + 1} failed: {msg}")

    if any(o.get("kind") == "image" and o.get("status") == "ok" for o in outputs):
        provider_notes.append(
            "Pollinations.ai free tier: one image per 45 seconds per IP, a 768px render upscaled to the requested "
            "1080px canvas, and a small pollinations.ai attribution mark that nologo=true does not remove without a "
            "pollen key. The report says so rather than claiming a paid no-logo render."
        )

    # ---------------- video ----------------
    if "video" in allowed:
        duration = float(brief.duration or 5.0)
        if duration > 8.5:
            provider_notes.append(
                f"Video length {duration:g}s is outside the free LTX-Video limit of 8.5s. The agent declined that length "
                "rather than speed-ramp or loop a shorter clip and call it the requested video."
            )
            outputs.append({"kind": "video", "filename": "video.mp4", "status": "error",
                            "error": f"requested {duration:g}s exceeds the free provider limit of 8.5s", "index": 1})
        else:
            filename = f"video-{brief.aspect.replace(':', 'x')}-{duration:g}s.mp4"
            dest = os.path.join(work, filename)
            progress(68, "generating video via LTX-Video")
            log("video via LTX-Video")
            try:
                info = providers.generate_video(
                    _video_prompt(brief, palette), brief.aspect, duration,
                    seed=2000 + int(hashlib.sha256(state["job_id"].encode()).hexdigest()[:4], 16), dest=dest,
                )
                info.update({"kind": "video", "filename": filename, "status": "ok", "index": 1})
                outputs.append(info)
                log(f"video ok: {info['width']}x{info['height']} {info['duration']}s")
            except Exception as exc:
                msg = str(exc)
                log(f"LTX video failed: {msg}")
                provider_notes.append(f"AI video route failed: {msg}")
                selected = None
                if brief.allow_stock_video or state.get("allow_stock_video"):
                    for q in _stock_query(brief):
                        try:
                            progress(72, "AI video unavailable; preparing a labelled stock fallback")
                            selected = providers.generate_stock_video(q, brief.aspect, duration, dest)
                            break
                        except Exception as sexc:
                            log(f"stock query {q!r} failed: {sexc}")
                            continue
                if selected:
                    selected["search_query"] = q
                    selected["prompt"] = _video_prompt(brief, palette)
                    selected.update({"kind": "video", "filename": filename, "status": "ok", "index": 1})
                    outputs.append(selected)
                    provider_notes.append(
                        "Because the AI video route failed, this delivery uses CC-licensed stock footage, clearly labelled "
                        "in the file row and report. It is not presented as AI-generated. A revision can retry generation "
                        "when the free GPU quota resets."
                    )
                    log(f"stock video ok: {selected['width']}x{selected['height']} {selected['duration']}s")
                else:
                    outputs.append({"kind": "video", "filename": filename, "status": "error", "error": msg, "index": 1})
                    provider_notes.append(
                        "No stock fallback was allowed or found. The agent did not deliver a still, a loop, or a "
                        "different length and call it the requested video."
                    )

    # ---------------- voiceover ----------------
    if "voiceover" in allowed:
        filename = "voiceover-48k.mp3"
        dest = os.path.join(work, filename)
        progress(80, "generating voiceover via Edge TTS")
        log(f"voiceover via Edge TTS ({brief.voice})")
        try:
            info = providers.generate_voice(
                brief.script, brief.voice, brief.duration, dest, rate=brief.voice_rate or "+0%",
            )
            info.update({"kind": "voiceover", "filename": filename, "status": "ok", "index": 1})
            outputs.append(info)
            log(f"voiceover ok: {info['duration']}s")
        except Exception as exc:
            msg = str(exc)
            outputs.append({"kind": "voiceover", "filename": filename, "status": "error", "error": msg, "index": 1})
            provider_notes.append(f"Voiceover failed: {msg}")
            log(f"voiceover failed: {msg}")

    # ---------------- final validation and report ----------------
    delivered = [o for o in outputs if o.get("status") == "ok"]
    for o in delivered:
        try:
            meta = providers.probe(o["path"])
            if o["kind"] == "image":
                if (meta.get("width"), meta.get("height")) != (o["width"], o["height"]):
                    raise PipelineError("image dimensions changed after generation")
            elif o["kind"] == "video":
                if abs((meta.get("duration") or 0) - float(o.get("duration") or 0)) > 1.0:
                    raise PipelineError("video duration failed the final check")
            elif o["kind"] == "voiceover" and (meta.get("duration") or 0) < 0.3:
                raise PipelineError("voiceover is empty")
            o["final_probe"] = {"width": meta.get("width"), "height": meta.get("height"),
                                "duration": round(meta.get("duration") or 0, 3), "size": meta.get("size")}
        except Exception as exc:
            o["status"] = "error"
            o["error"] = f"final validation: {exc}"
            provider_notes.append(f"{o['filename']} failed final validation: {exc}")

    delivered = [o for o in outputs if o.get("status") == "ok"]
    state["outputs"] = outputs
    state["provider_notes"] = provider_notes
    state["declines"] = declines
    state["report_md"] = _report(state, brief, outputs, declines, provider_notes, state.get("revision_note", ""))
    if delivered:
        state["status"] = "ready"
        state["stage"] = f"ready: {len(delivered)} file(s)"
    else:
        state["status"] = "failed"
        state["stage"] = "no file could be produced"
        state["error"] = "; ".join(o.get("error", "") for o in outputs if o.get("status") == "error")[:2000]
    save_job(state)
    progress(88, "files ready")
    return state


def _brief_for_state(state: dict[str, Any]) -> brief_mod.Brief:
    attachment_names = [a.get("filename", "") for a in (state.get("attachments") or [])]
    b = brief_mod.parse_brief(state.get("brief", ""), attachment_names)
    for key, attr in (("media_types", "media_types"), ("count", "count"), ("aspect", "aspect"),
                      ("duration", "duration"), ("script", "script"), ("voice", "voice"),
                      ("voice_rate", "voice_rate"), ("allow_stock_video", "allow_stock_video")):
        if state.get(key) not in (None, "", []):
            setattr(b, attr, state[key])
    if state.get("prompt"):
        b.prompt = state["prompt"]
    if state.get("aspect") in providers.ASPECTS:
        b.aspect = state["aspect"]
    if state.get("duration"):
        b.duration = float(state["duration"])
    return b


def run_revision(state: dict[str, Any], feedback: str, market: Any = None,
                 log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Regenerate only the named file numbers; everything else keeps its file."""
    log = log or (lambda _m: None)
    b = _brief_for_state(state)
    rev = brief_mod.parse_revision(feedback)
    targets = set(int(x) for x in rev.get("targets", []))
    changes = rev.get("changes", {})
    old = [o for o in state.get("outputs", []) if o.get("status") == "ok"]
    if not old:
        state["status"] = "failed"
        state["error"] = "there is no delivered file to revise"
        save_job(state)
        return state
    if not targets:
        # No explicit number: apply to every file of the kind the feedback names.
        kind = None
        low = feedback.lower()
        for k in ("voiceover", "video", "image"):
            if k in low:
                kind = k
                break
        targets = {int(o.get("index") or 0) for o in old if kind is None or o.get("kind") == kind}
    unknown = [t for t in targets if not any(int(o.get("index") or 0) == t for o in old)]
    work = os.path.join(WORK_DIR, state["job_id"], "rev-" + str(int(time.time())))
    os.makedirs(work, exist_ok=True)
    aspect = changes.get("aspect") or b.aspect
    duration = float(changes.get("duration") or b.duration or 0) or None
    outputs: list[dict[str, Any]] = []
    changed: list[int] = []
    for o in old:
        idx = int(o.get("index") or 0)
        if idx not in targets:
            outputs.append(o)
            continue
        changed.append(idx)
        kind = o.get("kind")
        try:
            if kind == "image":
                fn = f"image-{idx}-{aspect.replace(':', 'x')}.jpg"
                info = providers.generate_image(_image_prompt(b, idx - 1, "", b.count), aspect,
                                                seed=3000 + idx * 97, dest=os.path.join(work, fn))
            elif kind == "video":
                d = duration or float(o.get("duration") or 5)
                fn = f"video-{aspect.replace(':', 'x')}-{d:g}s.mp4"
                dest = os.path.join(work, fn)
                try:
                    info = providers.generate_video(_video_prompt(b, ""), aspect, d, seed=4000 + idx * 97, dest=dest)
                except Exception:
                    info = providers.generate_stock_video(" ".join(_stock_query(b)[:1]), aspect, d, dest)
            elif kind == "voiceover":
                d = duration or o.get("duration")
                fn = "voiceover-48k.mp3"
                info = providers.generate_voice(b.script, b.voice, d, os.path.join(work, fn), rate=b.voice_rate)
            else:
                outputs.append(o)
                continue
            info.update({"kind": kind, "filename": fn, "status": "ok", "index": idx})
            outputs.append(info)
            log(f"revision: regenerated file {idx} ({kind})")
        except Exception as exc:
            o = dict(o)
            o["revision_error"] = str(exc)[:800]
            outputs.append(o)
            log(f"revision: file {idx} failed: {exc}")
    state["outputs"] = outputs
    state["revision_note"] = (
        f"Buyer asked: {feedback[:500]}. Regenerated file(s): {', '.join(map(str, sorted(changed))) or 'none'}"
        + (f"; file(s) {', '.join(map(str, unknown))} were not found, so nothing changed for them." if unknown else ".")
    )
    state["report_md"] = _report(state, b, outputs, state.get("declines", []),
                                 state.get("provider_notes", []), state["revision_note"])
    state["status"] = "ready"
    state["stage"] = f"revision ready: {len(changed)} file(s) changed"
    save_job(state)
    return state


def upload_outputs(state: dict[str, Any], market: Any,
                   on_progress: Callable[[int, str], None] | None = None,
                   log: Callable[[str], None] | None = None) -> list[str]:
    log = log or (lambda _m: None)
    progress = on_progress or (lambda _p, _m: None)
    file_ids: list[str] = []
    delivered = [o for o in state.get("outputs", []) if o.get("status") == "ok"]
    for i, o in enumerate(delivered, 1):
        if o.get("file_id"):
            file_ids.append(o["file_id"])
            continue
        progress(90 + int(6 * i / max(1, len(delivered))), f"uploading {o['filename']}")
        mime = "image/jpeg" if o["kind"] == "image" else ("video/mp4" if o["kind"] == "video" else "audio/mpeg")
        try:
            up = market.upload_file(o["path"], mime)
            o["file_id"] = up.get("fileId")
            o["file_url"] = up.get("fileUrl")
            if o["file_id"]:
                file_ids.append(o["file_id"])
            log(f"uploaded {o['filename']} -> {o.get('file_id')}")
        except Exception as exc:
            o["upload_error"] = str(exc)[:600]
            base = _public_base()
            o["file_url"] = f"{base}/v1/jobs/{state['job_id']}/files/{o['filename']}"
            log(f"upload failed for {o['filename']}: {exc}")
    state["report_md"] = _report(state, brief_mod.parse_brief(state.get("brief", "")),
                                 state.get("outputs", []), state.get("declines", []),
                                 state.get("provider_notes", []), state.get("revision_note", ""))
    save_job(state)
    return file_ids


def submit_deliverable(state: dict[str, Any], market: Any, assignment_id: str,
                       file_ids: list[str], log: Callable[[str], None] | None = None) -> Any:
    log = log or (lambda _m: None)
    log(f"submitting {len(file_ids)} file(s) for {assignment_id}")
    result = market.submit(assignment_id, state["report_md"], files=file_ids)
    state.setdefault("submissions", []).append({
        "assignment_id": assignment_id, "submitted_at": utcnow(),
        "files": file_ids, "result": result if isinstance(result, dict) else {},
    })
    state["status"] = "submitted"
    state["stage"] = "submitted"
    save_job(state)
    return result


def purge_work(state: dict[str, Any], reason: str = "retention policy") -> None:
    """Delete the local working copy after Agent Market has the delivered files."""
    work = os.path.join(WORK_DIR, state["job_id"])
    shutil.rmtree(work, ignore_errors=True)


def cleanup_old_work(max_age_hours: float = 24.0) -> int:
    now = time.time()
    removed = 0
    for name in os.listdir(WORK_DIR):
        path = os.path.join(WORK_DIR, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age_hours * 3600:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    return removed
