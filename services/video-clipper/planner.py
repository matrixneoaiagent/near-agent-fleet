"""Brief parsing, highlight selection, boundary snapping and revision parsing."""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Any

import media


FORMAT_LABELS = {"9:16": "vertical 9:16", "1:1": "square 1:1", "16:9": "landscape 16:9"}


@dataclass
class Brief:
    raw: str
    video_url: str | None = None
    use_attachment: bool = False
    clip_count: int = 3
    formats: list[str] = field(default_factory=lambda: ["9:16"])
    captions: bool = False
    target_min: float | None = None
    target_max: float | None = None
    language: str | None = None
    retention_mode: str = "default"  # default | none
    wants_srt: bool = False
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
NON_VIDEO_HOSTS = (
    "market.near.ai",
    "docs.market.near.ai",
    "near.ai",
    "github.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "google.com",
)


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}\"'")


def _looks_like_video_url(url: str) -> bool:
    low = url.lower()
    if any(h in low for h in NON_VIDEO_HOSTS):
        return False
    if any(h in low for h in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv")):
        return True
    return bool(re.search(r"\.(mp4|mov|webm|mkv|m4v|avi|ogv|mpg|mpeg|m3u8)(\?|$)", low))


def parse_brief(text: str, has_attachment: bool = False) -> Brief:
    raw = text or ""
    low = raw.lower()
    brief = Brief(raw=raw)

    urls = [_clean_url(u) for u in URL_RE.findall(raw)]
    video_urls = [u for u in urls if _looks_like_video_url(u)]
    if video_urls:
        brief.video_url = video_urls[0]
    elif urls:
        brief.video_url = urls[0]
    if has_attachment:
        brief.use_attachment = True
    if not brief.video_url and not has_attachment:
        brief.errors.append(
            "I could not find a video link or an uploaded file in the brief. "
            "Send one public video URL (MP4/MOV/WebM or a public YouTube/Vimeo link) or attach the file."
        )

    # Clip count
    patterns = [
        r"(?:make|create|cut|produce|deliver|need|want|give me)\s+(\d{1,2})\s+(?:short[- ]form\s+)?(?:clips?|shorts?|videos?)",
        r"(\d{1,2})\s*[x×]\s*(?:clips?|shorts?|videos?)",
        r"(?:clip count|number of clips|clips?)\s*[:=]\s*(\d{1,2})",
        r"(\d{1,2})\s+(?:[a-z][a-z-]*\s+){0,3}(?:clips?|shorts?|videos?|squares?|reels?)\b",
    ]
    count = None
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            count = int(m.group(1))
            break
    if count is None:
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        for word, num in words.items():
            if re.search(rf"\b{word}\s+(?:short[- ]form\s+)?(?:clips?|shorts?|videos?)\b", low):
                count = num
                break
    brief.clip_count = max(1, min(10, count or 3))

    # Formats
    formats: list[str] = []
    if re.search(r"\ball\s+(?:three\s+)?formats?\b|\bevery\s+format\b", low):
        formats = ["9:16", "1:1", "16:9"]
    else:
        if re.search(r"\b9\s*[:x/]\s*16\b|\bvertical\b|\bportrait\b|\btiktok\b|\breels?\b|\bshorts?\b", low):
            formats.append("9:16")
        if re.search(r"\b1\s*[:x/]\s*1\b|\bsquares?\b", low):
            formats.append("1:1")
        if re.search(r"\b16\s*[:x/]\s*9\b|\blandscape\b|\bhorizontal\b|\bwidescreen\b", low):
            formats.append("16:9")
        # platform names only when no explicit aspect was given
        if not formats and re.search(r"\blinkedin\b", low):
            formats.append("1:1")
        if not formats and re.search(r"\byoutube\b", low) and not re.search(r"youtube\s+shorts?", low):
            formats.append("16:9")
    if not formats:
        formats = ["9:16"]
    brief.formats = list(dict.fromkeys(formats))

    # Captions
    if re.search(r"\b(no|without|skip|don't|do not)\s+(?:captions?|subtitles?)\b|\bcaptions?\s+(?:off|no)\b", low):
        brief.captions = False
    elif re.search(r"\bcaptions?\b|\bsubtitles?\b|\bcaptioned\b|\bburn(?:ed)?[- ]in\b|\btext on screen\b", low):
        brief.captions = True
    brief.wants_srt = bool(re.search(r"\bsrt\b|\bcaption file\b|\btranscript file\b", low))

    # Clip length
    m = re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:seconds?|secs?|s)\b", low)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        brief.target_min, brief.target_max = float(min(a, b)), float(max(a, b))
    else:
        m = re.search(r"(?:under|less than|max(?:imum)?|at most)\s*(\d{1,3})\s*(?:seconds?|secs?|s)\b", low)
        if m:
            brief.target_max = float(m.group(1))
        m = re.search(r"(\d{1,3})\s*(?:second|sec|s)\s*(?:clips?|long|each)\b", low)
        if m:
            brief.target_max = float(m.group(1))
            brief.target_min = max(8.0, float(m.group(1)) * 0.7)

    # Language hints
    languages = {
        "english": "en",
        "spanish": "es",
        "french": "fr",
        "german": "de",
        "italian": "it",
        "portuguese": "pt",
        "romanian": "ro",
        "russian": "ru",
        "ukrainian": "uk",
        "arabic": "ar",
        "hindi": "hi",
        "japanese": "ja",
        "korean": "ko",
        "chinese": "zh",
        "mandarin": "zh",
    }
    for name, code in languages.items():
        if re.search(rf"\b(?:in|speaks?|language[:\s]+)\s*{name}\b", low):
            brief.language = code
            break
    if brief.language is None and re.search(r"\benglish\b", low):
        brief.language = "en"

    # Retention / privacy
    if re.search(r"\bno[- ]retention(?!\s+data)\b|\bdelete\s+(?:the\s+)?(?:source|original|video|files?)\s+(?:right\s+)?after\b|\bdo not keep\b|\bdon't keep\b|\bpurge\b", low):
        brief.retention_mode = "none"

    if brief.clip_count > 1 and brief.target_max and brief.target_max < 5:
        brief.notes.append("Requested clip length is very short; I keep complete sentences, so clips may be a little longer.")

    return brief


# --------------------------------------------------------------------------
# Feature building


def _resample(values: list[float], in_bin: float, out_bin: float = 1.0) -> list[float]:
    if not values:
        return []
    out_len = max(1, int(math.ceil(len(values) * in_bin / out_bin)))
    out = []
    for i in range(out_len):
        a = int(i * out_bin / in_bin)
        b = max(a + 1, int((i + 1) * out_bin / in_bin))
        chunk = values[a:b]
        out.append(sum(chunk) / len(chunk) if chunk else 0.0)
    return out


def _percentile(sorted_values: list[float], v: float) -> float:
    if not sorted_values:
        return 0.0
    i = bisect.bisect_right(sorted_values, v)
    return max(0.0, min(1.0, i / len(sorted_values)))


def _rolling(values: list[float], radius: int) -> list[float]:
    if not values:
        return []
    out = []
    for i in range(len(values)):
        a = max(0, i - radius)
        b = min(len(values), i + radius + 1)
        out.append(sum(values[a:b]) / (b - a))
    return out


def _interp_heat(heat: list[dict[str, float]], t: float) -> float:
    if not heat:
        return 0.0
    # heat entries are few hundred; linear scan is fine, but use bisect on starts
    starts = [h["start"] for h in heat]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        i = 0
    row = heat[i]
    if row["end"] > row["start"] and t <= row["end"]:
        return max(0.0, min(1.0, row["value"]))
    return max(0.0, min(1.0, row["value"]))


@dataclass
class Analysis:
    duration: float
    energy: list[float]
    motion: list[float]
    scenes: list[tuple[float, float]]
    silences: list[tuple[float, float]]
    heat: list[dict[str, float]]
    heat_source: str | None
    speech_intervals: list[tuple[float, float]]
    speech_fraction: list[float]
    score: list[float]
    energy_pct: list[float]
    motion_pct: list[float]
    scene_count: list[int]
    has_audio: bool = True


def build_analysis(
    path: str,
    duration: float,
    has_audio: bool,
    heat: list[dict[str, float]] | None = None,
    heat_source: str | None = None,
) -> Analysis:
    energy_raw = media.audio_energy(path) if has_audio else []
    energy = _resample(energy_raw, 0.5, 1.0) if energy_raw else [0.0] * max(1, int(duration))
    if len(energy) < duration:
        energy += [energy[-1] if energy else 0.0] * (int(duration) - len(energy) + 1)
    motion_raw = media.motion_series(path)
    motion = _resample(motion_raw, 0.5, 1.0)
    if len(motion) < duration:
        motion += [motion[-1] if motion else 0.0] * (int(duration) - len(motion) + 1)
    try:
        scenes = media.scene_cuts(path)
    except Exception:
        scenes = []
    silences = media.detect_silences(path) if has_audio else []

    # speech intervals = complement of silences within duration
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in sorted(silences):
        if s > cursor:
            speech.append((cursor, min(s, duration)))
        cursor = max(cursor, e)
    if cursor < duration:
        speech.append((cursor, duration))

    secs = max(1, int(math.ceil(duration)))
    speech_fraction = []
    for i in range(secs):
        a, b = float(i), float(min(i + 1, duration))
        total = 0.0
        for s, e in speech:
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                total += hi - lo
        speech_fraction.append(total / max(0.01, b - a))

    # percentile ranks
    energy_sorted = sorted(energy[:secs])
    motion_sorted = sorted(motion[:secs])
    energy_pct = [_percentile(energy_sorted, v) for v in energy[:secs]]
    motion_pct = [_percentile(motion_sorted, v) for v in motion[:secs]]

    scene_count = [0] * secs
    for t, _score in scenes:
        idx = int(t)
        if 0 <= idx < secs:
            scene_count[idx] += 1
    scene_count = _rolling([float(x) for x in scene_count], 4)

    energy_smooth = _rolling(energy_pct, 2)
    motion_smooth = _rolling(motion_pct, 2)
    speech_smooth = _rolling(speech_fraction, 3)
    heat_values = [_interp_heat(heat or [], i) for i in range(secs)]
    heat_smooth = _rolling(heat_values, 2)
    if heat and max(heat_values or [0.0]) <= 0.0:
        heat = []
        heat_source = None
        heat_values = [0.0] * secs
        heat_smooth = [0.0] * secs

    score = []
    for i in range(secs):
        s = (
            1.15 * energy_smooth[i]
            + 0.85 * min(1.0, scene_count[i] / 3.0)
            + 0.70 * motion_smooth[i]
            + 0.55 * speech_smooth[i]
            + 0.35 * min(1.0, scene_count[i] / 6.0)
        )
        if heat:
            s += 4.0 * heat_smooth[i]
        score.append(s)

    return Analysis(
        duration=duration,
        energy=energy[:secs],
        motion=motion[:secs],
        scenes=scenes,
        silences=silences,
        heat=heat or [],
        heat_source=heat_source,
        speech_intervals=speech,
        speech_fraction=speech_fraction[:secs],
        score=score,
        energy_pct=energy_pct,
        motion_pct=motion_pct,
        scene_count=scene_count,
        has_audio=has_audio,
    )


# --------------------------------------------------------------------------
# Selection and boundaries


@dataclass
class Candidate:
    start: float
    end: float
    raw_start: float
    raw_end: float
    score: float
    reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    transcript: str = ""
    words: list[dict[str, Any]] = field(default_factory=list)
    cues: list[dict[str, Any]] = field(default_factory=list)


def _snap_to_silence_start(t: float, silences: list[tuple[float, float]], window: float = 4.0) -> float | None:
    best = None
    best_d = window
    for s, e in silences:
        # start at the end of a silence = speech onset
        for cand in (e, s):
            d = abs(cand - t)
            if d < best_d:
                best_d = d
                best = cand
    return best


def _snap_to_silence_end(t: float, silences: list[tuple[float, float]], window: float = 4.0) -> float | None:
    best = None
    best_d = window
    for s, e in silences:
        for cand in (s, e):
            d = abs(cand - t)
            if d < best_d:
                best_d = d
                best = cand
    return best


def _sentence_starts(words: list[dict[str, Any]]) -> list[float]:
    starts: list[float] = []
    for i, w in enumerate(words):
        prev = words[i - 1]["text"] if i > 0 else ""
        if i == 0 or re.search(r"[.!?][\"')\]]?$", prev):
            starts.append(w["start"])
    return starts


def _pause_starts(words: list[dict[str, Any]]) -> list[float]:
    starts: list[float] = []
    for i, w in enumerate(words):
        prev = words[i - 1] if i > 0 else None
        gap = (w["start"] - prev["end"]) if prev else 99.0
        if i == 0 or gap >= 0.65:
            starts.append(w["start"])
    return starts


def _sentence_ends(words: list[dict[str, Any]]) -> list[float]:
    ends: list[float] = []
    for w in words:
        if re.search(r"[.!?][\"')\]]?$", w.get("text", "")):
            ends.append(w["end"])
    return ends


def _pause_ends(words: list[dict[str, Any]]) -> list[float]:
    ends: list[float] = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 99.0
        if gap >= 0.65:
            ends.append(w["end"])
    return ends


def snap_start(t: float, words: list[dict[str, Any]], silences: list[tuple[float, float]], duration: float) -> float:
    t = max(0.0, min(t, duration - 1.0))
    # Prefer a real sentence start (punctuation) when one is close.
    for candidates in (_sentence_starts(words), _pause_starts(words)):
        nearby = [x for x in candidates if -12.0 <= x - t <= 6.0]
        if nearby:
            return max(0.0, min(nearby, key=lambda x: abs(x - t)) - 0.08)
    candidates = [w for w in words if abs(w["start"] - t) <= 3.0]
    if candidates:
        w = min(candidates, key=lambda x: abs(x["start"] - t))
        return max(0.0, w["start"] - 0.12)
    s = _snap_to_silence_start(t, silences)
    if s is not None:
        return max(0.0, s - 0.05)
    return t


def snap_end(t: float, words: list[dict[str, Any]], silences: list[tuple[float, float]], duration: float) -> float:
    t = max(1.0, min(t, duration))
    # Prefer a real sentence end (punctuation) over a mid-sentence pause.
    for candidates in (_sentence_ends(words), _pause_ends(words)):
        after = [x for x in candidates if 0.0 <= x - t <= 45.0]
        if after:
            return min(duration, min(after) + 0.20)
        before = [x for x in candidates if 0.0 <= t - x <= 5.0]
        if before:
            return min(duration, max(before) + 0.20)
    candidates = [w for w in words if abs(w["end"] - t) <= 3.0]
    if candidates:
        w = min(candidates, key=lambda x: abs(x["end"] - t))
        return min(duration, w["end"] + 0.25)
    s = _snap_to_silence_end(t, silences)
    if s is not None:
        return min(duration, s + 0.10)
    return t


def _window_metrics(analysis: Analysis, start: float, end: float) -> dict[str, float]:
    a, b = int(max(0, start)), int(min(len(analysis.score), math.ceil(end)))
    if b <= a:
        b = min(len(analysis.score), a + 1)
    energy = sum(analysis.energy_pct[a:b]) / max(1, b - a)
    motion = sum(analysis.motion_pct[a:b]) / max(1, b - a)
    speech = sum(analysis.speech_fraction[a:b]) / max(1, b - a)
    scenes = sum(analysis.scene_count[a:b])
    heat_peak = max((_interp_heat(analysis.heat, t) for t in range(a, b)), default=0.0)
    score_mean = sum(analysis.score[a:b]) / max(1, b - a)
    score_sorted = sorted(analysis.score)
    score_pct = _percentile(score_sorted, score_mean) * 100
    score_peak = max(analysis.score[a:b]) if b > a else 0.0
    score_peak_pct = _percentile(score_sorted, score_peak) * 100
    energy_peak = max(analysis.energy_pct[a:b]) if b > a else 0.0
    motion_peak = max(analysis.motion_pct[a:b]) if b > a else 0.0
    return {
        "energy_pct": energy * 100,
        "motion_pct": motion * 100,
        "speech_pct": speech * 100,
        "scene_cuts": scenes,
        "heat_peak": heat_peak,
        "score_pct": score_pct,
        "score_peak_pct": score_peak_pct,
        "energy_peak_pct": energy_peak * 100,
        "motion_peak_pct": motion_peak * 100,
    }


def _top_heat_time(analysis: Analysis, start: float, end: float) -> tuple[float, float]:
    best_t, best_v = start, 0.0
    t = start
    while t <= end:
        v = _interp_heat(analysis.heat, t)
        if v > best_v:
            best_v, best_t = v, t
        t += 0.5
    return best_t, best_v


def make_reason(analysis: Analysis, start: float, end: float, transcript: str = "") -> tuple[str, dict[str, float]]:
    m = _window_metrics(analysis, start, end)
    if analysis.heat:
        ht, hv = _top_heat_time(analysis, start, end)
        reason = (
            f"YouTube's public most-replayed data peaks at {fmt_time(ht)} "
            f"(intensity {hv:.2f} of 1.00), inside this window. "
            f"The clip covers that peak and the surrounding high-engagement passage."
        )
        if transcript:
            reason += f' It opens with: "{short_quote(transcript)}"'
        return reason, m
    bits = [
        f"the window contains a combined highlight peak in the top {max(1, round(100 - m['score_peak_pct']))}% of the video",
    ]
    if analysis.has_audio and max(analysis.energy_pct or [0]) > 0:
        bits.append(f"audio energy peak in the top {max(1, round(100 - m['energy_peak_pct']))}%")
    if analysis.scene_count and max(analysis.scene_count or [0]) > 0:
        bits.append(f"{int(round(m['scene_cuts']))} scene change(s)")
    if analysis.motion_pct and max(analysis.motion_pct or [0]) > 0:
        bits.append(f"motion peak in the top {max(1, round(100 - m['motion_peak_pct']))}%")
    if analysis.has_audio and analysis.speech_fraction and max(analysis.speech_fraction or [0]) > 0:
        bits.append(f"speech covers {round(m['speech_pct'])}% of the window")
    if len(bits) == 1:
        bits.append("visual scene structure")
    reason = (
        "No retention or 'most replayed' data was available for this source, so this was "
        "chosen by content analysis: " + ", ".join(bits) + "."
    )
    if transcript:
        reason += f' It opens with: "{short_quote(transcript)}"'
    return reason, m


def short_quote(text: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def select_candidates(analysis: Analysis, brief: Brief) -> list[Candidate]:
    duration = analysis.duration
    count = brief.clip_count
    if brief.target_min and brief.target_max:
        target = (brief.target_min + brief.target_max) / 2
    elif brief.target_max:
        target = brief.target_max
    else:
        target = duration / max(1, count) / 8.0
        target = max(20.0, min(60.0, target))
        if duration < 150:
            target = max(8.0, min(30.0, duration / max(1, count) * 0.8))
    target = max(6.0, min(90.0, target))
    min_len = max(5.0, min(target * 0.55, brief.target_min or target * 0.55))
    max_len = max(min_len + 1.0, min(95.0, brief.target_max or target * 1.55))

    step = max(1.0, target / 8.0)
    max_start = max(0.0, duration - target)
    windows: list[tuple[float, float, float]] = []
    t = 0.0
    while t <= max_start + 0.01:
        a, b = t, min(duration, t + target)
        if b - a >= min_len:
            aa, bb = int(a), int(math.ceil(b))
            vals = analysis.score[aa:bb]
            if vals:
                vals_sorted = sorted(vals, reverse=True)
                keep = max(1, len(vals_sorted) // 2)
                s = sum(vals_sorted[:keep]) / keep
                windows.append((a, b, s))
        t += step
    windows.sort(key=lambda x: x[2], reverse=True)

    selected: list[Candidate] = []
    for a, b, s in windows:
        if len(selected) >= count:
            break
        overlap = False
        for c in selected:
            inter = max(0.0, min(b, c.end) - max(a, c.start))
            if inter > 0.35 * min(b - a, c.end - c.start):
                overlap = True
                break
            if abs(a - c.start) < target * 0.55:
                overlap = True
                break
        if overlap:
            continue
        selected.append(Candidate(start=a, end=b, raw_start=a, raw_end=b, score=s))
    if not selected and windows:
        selected = [Candidate(start=windows[0][0], end=windows[0][1], raw_start=windows[0][0], raw_end=windows[0][1], score=windows[0][2])]
    selected.sort(key=lambda c: c.start)
    for c in selected:
        c.metrics = _window_metrics(analysis, c.start, c.end)
    return selected


def snap_exact_boundaries(start: float, end: float, words: list[dict[str, Any]]) -> tuple[float, float]:
    """Respect a requested cut time unless it lands inside a word."""
    for w in words:
        if w["start"] + 0.02 < start < w["end"] - 0.02:
            start = w["start"]
            break
    for w in words:
        if w["start"] + 0.02 < end < w["end"] - 0.02:
            end = w["end"] + 0.12
            break
    return start, end


def _inside_word(x: float, words: list[dict[str, Any]], pad: float = 0.03) -> bool:
    return any(w["start"] + pad < x < w["end"] - pad for w in words)


def _remove_overlaps(cands: list[Candidate], analysis: Analysis) -> list[Candidate]:
    """Trim adjacent clips at the cleanest boundary so they never overlap."""
    cands = sorted(cands, key=lambda c: c.start)
    for i in range(1, len(cands)):
        prev, cur = cands[i - 1], cands[i]
        if cur.start >= prev.end - 0.05:
            continue
        mid = (prev.end + cur.start) / 2.0
        # Prefer a complete sentence boundary, even if it is a little farther
        # from the midpoint: a shorter complete clip beats a mid-sentence cut.
        sentence_edges = [
            x
            for x in _sentence_ends(prev.words) + _sentence_starts(cur.words)
            if prev.start + 3.0 <= x <= cur.end - 3.0 and abs(x - mid) <= 25.0
        ]
        pause_edges = [
            x
            for x in _pause_ends(prev.words) + _pause_starts(cur.words)
            if prev.start + 3.0 <= x <= cur.end - 3.0 and abs(x - mid) <= 12.0
        ]
        if sentence_edges:
            boundary = min(sentence_edges, key=lambda x: abs(x - mid))
        elif pause_edges:
            boundary = min(pause_edges, key=lambda x: abs(x - mid))
        else:
            edges = sorted({round(w[k], 3) for w in prev.words + cur.words for k in ("start", "end")})
            safe = [
                x
                for x in edges
                if prev.start + 3.0 <= x <= cur.end - 3.0
                and abs(x - mid) <= 8.0
                and not _inside_word(x, prev.words)
                and not _inside_word(x, cur.words)
            ]
            boundary = min(safe, key=lambda x: abs(x - mid)) if safe else mid
        prev.end = max(prev.start + 3.0, boundary)
        cur.start = min(cur.end - 3.0, boundary)
    return cands


def refine_boundaries(
    cand: Candidate,
    analysis: Analysis,
    brief: Brief,
    source_path: str,
    work_dir: str,
    transcribe: bool,
) -> Candidate:
    duration = analysis.duration
    words: list[dict[str, Any]] = []
    transcript = ""
    if transcribe:
        try:
            asr = media.transcribe_range(
                source_path,
                max(0.0, cand.start - 12.0),
                min(duration, cand.end + 35.0),
                language=brief.language,
                work_dir=work_dir,
            )
            words = asr.get("words", []) or []
            transcript = asr.get("text", "") or ""
        except Exception:
            words = []
            transcript = ""

    # If no words, try narrow boundary transcriptions to avoid mid-word cuts.
    if not words and analysis.speech_intervals:
        for t in (cand.start, cand.end):
            try:
                asr = media.transcribe_range(
                    source_path,
                    max(0.0, t - 3.5),
                    min(duration, t + 3.5),
                    language=brief.language,
                    work_dir=work_dir,
                )
                words.extend(asr.get("words", []) or [])
            except Exception:
                pass

    start = snap_start(cand.raw_start, words, analysis.silences, duration)
    end = snap_end(cand.raw_end, words, analysis.silences, duration)
    if end - start < max(5.0, min(10.0, analysis.duration / 10.0)):
        # expand to the next available boundary
        end = min(duration, start + max(8.0, (cand.raw_end - cand.raw_start)))
    if end - start > 95.0:
        end = start + 95.0
    if start < 0.5 and duration > 10:
        start = 0.5
    if end > duration - 0.3 and duration > 10:
        end = duration - 0.3
    if end <= start:
        end = min(duration, start + 5.0)

    # Sentence-boundary validation: if the end lands on a word with no gap,
    # move to the end of the nearest complete word/sentence within 2.5s. If
    # the cut already sits on a recognised sentence boundary, keep it even
    # when Whisper gives the next word an early start timestamp.
    if words:
        sentence_ends = _sentence_ends(words) + _pause_ends(words)
        sentence_starts = _sentence_starts(words) + _pause_starts(words)
        end_is_sentence = any(abs(end - (x + 0.20)) <= 0.08 for x in sentence_ends)
        start_is_sentence = any(abs(start - (x - 0.08)) <= 0.08 for x in sentence_starts)
        if not end_is_sentence:
            overlapping = [w for w in words if w["start"] < end < w["end"] - 0.05]
            if overlapping:
                w = overlapping[0]
                end = min(duration, w["end"] + 0.25)
        if not start_is_sentence:
            start_overlap = [w for w in words if w["start"] < start < w["end"] - 0.05]
            if start_overlap:
                start = max(0.0, start_overlap[0]["start"] - 0.12)

    # Keep the delivered length near the requested window. If the nearest
    # complete sentence overshoots, use the latest complete sentence that
    # still fits the requested maximum.
    requested_max = brief.target_max or 60.0
    max_allowed = min(95.0, max(12.0, requested_max * 1.25))
    if words and end - start > max_allowed:
        ends = [x for x in (_sentence_ends(words) + _pause_ends(words)) if start + 8.0 <= x <= start + max_allowed]
        if ends:
            end = min(duration, max(ends) + 0.20)
    if end - start > 95.0:
        end = start + 95.0

    cand.start, cand.end = start, end
    cand.words = words
    cand.transcript = transcript
    return cand


def build_cues(cand: Candidate, fmt: str) -> list[dict[str, Any]]:
    if fmt == "9:16":
        return media.group_words_into_cues(cand.words, cand.start, cand.end, max_words=7, max_chars=40)
    if fmt == "1:1":
        return media.group_words_into_cues(cand.words, cand.start, cand.end, max_words=8, max_chars=46)
    return media.group_words_into_cues(cand.words, cand.start, cand.end, max_words=10, max_chars=62)


# --------------------------------------------------------------------------
# Revision parsing


def parse_timecode(text: str) -> float | None:
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
    if not m:
        return None
    if m.group(3) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def _clip_indices(text: str, clip_count: int) -> list[int]:
    idx: set[int] = set()
    for m in re.finditer(r"\bclip\s*#?\s*(\d{1,2})\b", text):
        idx.add(int(m.group(1)) - 1)
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\s+clip\b", text):
        idx.add(int(m.group(1)) - 1)
    words = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5, "seventh": 6, "eighth": 7, "ninth": 8, "tenth": 9}
    for word, i in words.items():
        if re.search(rf"\b{word}\s+clip\b", text):
            idx.add(i)
    for m in re.finditer(r"\bclips?\s+(\d{1,2})\s*(?:and|,|&)\s*(\d{1,2})\b", text):
        idx.add(int(m.group(1)) - 1)
        idx.add(int(m.group(2)) - 1)
    return sorted(i for i in idx if 0 <= i < clip_count)


def parse_revision(feedback: str, clip_count: int) -> dict[str, Any]:
    text = (feedback or "").lower()
    targets = _clip_indices(text, clip_count)
    actions: dict[int, dict[str, Any]] = {i: {} for i in targets}
    global_actions: dict[str, Any] = {}
    sentences = re.split(r"(?<=[.!?])\s+|\n+", feedback or "")

    def parse_sentence(s_low: str) -> dict[str, Any]:
        action: dict[str, Any] = {}
        # Absolute start/end times, each anchored to its own keyword.
        m = re.search(r"\bstart\b[^0-9]{0,30}(\d{1,2}:\d{2}(?::\d{2})?)", s_low)
        if m:
            action["start_abs"] = parse_timecode(m.group(1))
        m = re.search(r"\bend\b[^0-9]{0,30}(\d{1,2}:\d{2}(?::\d{2})?)", s_low)
        if m:
            action["end_abs"] = parse_timecode(m.group(1))
        m = re.search(r"\bstart\b.*?(\d{1,3})\s*(?:seconds?|secs?|s)\s*(earlier|sooner|later|after|before)", s_low)
        if m:
            delta = float(m.group(1))
            action["start_delta"] = -delta if m.group(2) in ("earlier", "sooner", "before") else delta
        m = re.search(r"\bend\b.*?(\d{1,3})\s*(?:seconds?|secs?|s)\s*(earlier|sooner|later|after|before)", s_low)
        if m:
            delta = float(m.group(1))
            action["end_delta"] = -delta if m.group(2) in ("earlier", "sooner", "before") else delta
        m = re.search(
            r"(?:make|change|trim|cut|shorten|set|length)\s*(?:it|the clip|clip \d+)?\s*(?:to|at|of)?\s*(\d{1,3})\s*(?:seconds?|secs?|s)\b",
            s_low,
        )
        if m:
            action["duration"] = float(m.group(1))
        m = re.search(r"(?:trim|cut)\s+(?:the\s+)?(?:first|start)\s+(\d{1,3})\s*(?:seconds?|secs?|s)", s_low)
        if m:
            action["start_delta"] = float(m.group(1))
        m = re.search(r"(?:trim|cut)\s+(?:the\s+)?(?:last|end)\s+(\d{1,3})\s*(?:seconds?|secs?|s)", s_low)
        if m:
            action["end_delta"] = -float(m.group(1))
        if re.search(r"\b(add|include|with|burn in|burned in)\s+(?:captions?|subtitles?)\b", s_low):
            action["captions"] = True
        if re.search(r"\b(remove|drop|without|no)\s+(?:captions?|subtitles?)\b", s_low):
            action["captions"] = False
        fmt = None
        if re.search(r"\b9\s*[:x/]\s*16\b|\bvertical\b|\bportrait\b|\btiktok\b|\breels?\b", s_low):
            fmt = "9:16"
        elif re.search(r"\b1\s*[:x/]\s*1\b|\bsquares?\b", s_low):
            fmt = "1:1"
        elif re.search(r"\b16\s*[:x/]\s*9\b|\blandscape\b|\bhorizontal\b", s_low):
            fmt = "16:9"
        if fmt:
            action["format"] = fmt
        if re.search(r"\bredo\b|\bremake\b|\breplace\b|\bredraw\b|\bre-?cut\b|\btry again\b|\bfix\b", s_low):
            action["redo"] = True
        return action

    for sentence in sentences:
        s_low = sentence.lower()
        if not s_low.strip():
            continue
        if re.search(r"^(keep|leave|don't change|do not change|no change)\b", s_low) or re.search(
            r"\bunchanged\b", s_low
        ):
            continue
        idxs = _clip_indices(s_low, clip_count)
        action = parse_sentence(s_low)
        if idxs:
            for i in idxs:
                actions.setdefault(i, {}).update(action)
            if not action:
                for i in idxs:
                    actions.setdefault(i, {})["redo"] = True
        else:
            global_actions.update(action)

    targets = sorted(i for i, action in actions.items() if action)
    return {"targets": targets, "actions": actions, "global": global_actions, "raw": feedback}
