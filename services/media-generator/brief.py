"""Brief parsing and the content rules the listing promises to enforce."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

ASPECT_WORDS = [
    (r"\b(?:square|1\s*:\s*1|one[ -]by[ -]one)\b", "1:1"),
    (r"\b(?:vertical|portrait|9\s*:\s*16|story|stories|reel|reels|tiktok|shorts?)\b", "9:16"),
    (r"\b(?:landscape|horizontal|widescreen|16\s*:\s*9|youtube)\b", "16:9"),
]

DURATION_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*(?:-|\s)?\s*(?:seconds?|secs?|s)\b"
    r"|(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*-\s*second\b",
    re.I,
)

MEDIA_WORDS = {
    "image": r"\b(?:images?|photos?|pictures?|renders?|illustrations?|graphics?|posters?|thumbnails?|product shots?)\b",
    "video": r"\b(?:videos?|clips?|footage|reels?|\bshorts\b|animations?|b-?roll)\b",
    "voiceover": r"\b(?:voice[ -]?over|voiceover|narration|narrate|read (?:this|the) (?:script|text)|spoken audio|audio track|tts)\b",
}

STYLE_WORDS = re.compile(
    r"\b(?:photorealistic|photo-?realistic|realistic|cinematic|studio lighting|soft lighting|natural light|"
    r"minimalist|product photo|commercial photography|8k|4k|high detail|shallow depth of field|"
    r"watercolor|illustration|3d render|flat design|vintage|moody|bright|clean background)\b",
    re.I,
)


@dataclass
class Violation:
    part: str
    rule: str
    reason: str


@dataclass
class Brief:
    text: str
    media_types: list[str] = field(default_factory=list)
    count: int = 1
    aspect: str = "1:1"
    duration: float | None = None
    prompt: str = ""
    script: str = ""
    voice: str = "warm_female"
    voice_rate: str = "+0%"
    language: str = "en"
    reference_urls: list[str] = field(default_factory=list)
    allow_stock_video: bool = False
    no_retention: bool = False
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["violations"] = [asdict(v) for v in self.violations]
        return d

    @property
    def allowed_types(self) -> list[str]:
        blocked = {v.part for v in self.violations if v.part in ("all", "image", "video", "voiceover")}
        return [t for t in self.media_types if t not in blocked and "all" not in blocked]


def _count(text: str, media: str) -> int:
    # Remove durations first: "8-second video" is one video, not eight.
    text = re.sub(r"\b\d{1,3}(?:\.\d+)?\s*(?:-|\s)?\s*(?:seconds?|secs?|s)\b", "", text, flags=re.I)
    noun = {"image": r"(?:images?|photos?|pictures?|renders?|variations?|versions?)",
            "video": r"(?:videos?|clips?)",
            "voiceover": r"(?:voiceovers?|takes?|versions?)"}[media]
    m = re.search(rf"\b(\d{{1,2}}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:[\w-]+\s+){{0,3}}{noun}\b", text, re.I)
    if m:
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else WORD_NUMBERS.get(raw, 1)
        return max(1, min(n, 6 if media == "image" else 3))
    m = re.search(rf"\b{noun}\s*(?:x|×)\s*(\d{{1,2}})\b", text, re.I)
    if m:
        return max(1, min(int(m.group(1)), 6 if media == "image" else 3))
    return 1


def _aspect(text: str, media: str) -> str:
    for pattern, value in ASPECT_WORDS:
        if re.search(pattern, text, re.I):
            return value
    if re.search(r"\b(?:coffee|drink|food|product|bottle|cup|mug)\b", text, re.I) and media == "image":
        return "1:1"
    return "9:16" if media == "video" else "1:1"


def _duration(text: str, media: str) -> float | None:
    # Only treat a duration as the deliverable length when it is attached to
    # video/audio words, so "3 product photos" never becomes a 3-second video.
    window = text
    if media == "image":
        return None
    matches = list(DURATION_RE.finditer(window))
    if not matches:
        return None
    for m in matches:
        start, end = m.span()
        around = text[max(0, start - 40): min(len(text), end + 40)].lower()
        if any(w in around for w in ("video", "clip", "second", "voice", "audio", "narration", "voiceover", "length", "long")):
            value = float(m.group(1) or m.group(2))
            if 1 <= value <= 120:
                return value
    return None


def _strip_instructions(text: str, media: str) -> str:
    s = text.strip()
    s = re.sub(r"^(?:please\s+)?(?:make|create|generate|produce|give me|i need|i want|can you)\s+", "", s, flags=re.I)
    s = re.sub(r"\b(?:\d{1,2}|one|two|three|four|five|six)\s+(?:[\w-]+\s+){0,3}"
               r"(?:images?|photos?|pictures?|renders?|variations?|versions?|videos?|clips?|voiceovers?)\b", "", s, flags=re.I)
    s = re.sub(r"\b(?:square|vertical|portrait|landscape|horizontal|widescreen|9\s*:\s*16|16\s*:\s*9|1\s*:\s*1)\b", "", s, flags=re.I)
    s = re.sub(r"\b\d{1,3}(?:\.\d+)?\s*(?:-|\s)?\s*(?:seconds?|secs?|s)\b", "", s, flags=re.I)
    s = re.sub(r"\b(?:please|thanks|thank you)\b", "", s, flags=re.I)
    s = re.sub(r",?\s*(?:stock|b-?roll|licensed footage)\s+is\s+(?:fine|ok|okay|acceptable).*$", "", s, flags=re.I)
    s = re.sub(r",?\s*if\s+(?:ai\s+)?(?:generation|video generation)\s+is\s+unavailable.*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:-")
    # "product photos of X" -> "X"
    m = re.search(r"\bof\s+(.+)$", s, re.I)
    if m and len(m.group(1)) > 3:
        s = m.group(1).strip()
    return s.strip(" ,.;:-") or text.strip()


def _voice(text: str) -> tuple[str, str, str]:
    lower = text.lower()
    key = "warm_female"
    if re.search(r"\b(?:british|uk|english accent)\b", lower):
        key = "british_male" if re.search(r"\bmale\b", lower) else "british_female"
    elif re.search(r"\bspanish\b", lower):
        key = "spanish_female"
    elif re.search(r"\bfrench\b", lower):
        key = "french_female"
    elif re.search(r"\bgerman\b", lower):
        key = "german_female"
    elif re.search(r"\b(?:deep|low)\b.*\bmale\b|\bmale\b.*\b(?:deep|low)\b", lower):
        key = "deep_male"
    elif re.search(r"\bmale\b|\bman\b", lower):
        key = "male"
    elif re.search(r"\b(?:energetic|upbeat|excited)\b", lower):
        key = "energetic_female"
    elif re.search(r"\b(?:calm|soothing|gentle)\b", lower):
        key = "calm_female"
    elif re.search(r"\bwarm\b", lower) and re.search(r"\bfemale\b|\bwoman\b", lower):
        key = "warm_female"
    elif re.search(r"\bfemale\b|\bwoman\b", lower):
        key = "female"
    rate = "+0%"
    if re.search(r"\b(?:slow|slower|calm)\b", lower):
        rate = "-8%"
    elif re.search(r"\b(?:fast|faster|energetic|upbeat)\b", lower):
        rate = "+8%"
    lang = "en"
    for code, word in (("es", "spanish"), ("fr", "french"), ("de", "german"), ("it", "italian"), ("pt", "portuguese")):
        if word in lower:
            lang = code
            break
    return key, rate, lang


def _script(text: str) -> str:
    # Quoted script wins.
    m = re.search(r"[\"“](.{10,4000}?)[\"”]", text, re.S)
    if m and re.search(r"script|voice|narrat|read", text[:m.start()], re.I):
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"(?:script|voiceover text|voice-over text|narration text|read(?: this| the following)?)\s*[:\-]\s*(.+)", text, re.I | re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    # Cut a trailing meta-instruction line, not words inside the script.
    body = re.split(r"\n\s*(?:voice|tone|style|duration|format|deliver|aspect|language|use a|with a)\b", body, maxsplit=1, flags=re.I)[0]
    body = re.sub(r"\s*(?:in a|with a|using a)\s+(?:warm|calm|deep|soft|bright|energetic|professional|friendly)?\s*(?:female|male|woman|man|narrator)?\s*voice.*$", "", body, flags=re.I)
    body = re.sub(r"\s*\b\d{1,3}\s*(?:-|\s)?\s*(?:seconds?|secs?)\b.*$", "", body, flags=re.I)
    return re.sub(r"\s+", " ", body).strip(" \n\"“”")


CELEBRITIES = [
    "tom cruise", "brad pitt", "angelina jolie", "johnny depp", "leonardo dicaprio", "scarlett johansson",
    "taylor swift", "beyonce", "rihanna", "drake", "elon musk", "bill gates", "jeff bezos", "mark zuckerberg",
    "donald trump", "joe biden", "barack obama", "hillary clinton", "vladimir putin", "xi jinping",
    "kim kardashian", "kanye west", "oprah", "morgan freeman", "keanu reeves", "dwayne johnson", "the rock",
    "michael jackson", "princess diana", "queen elizabeth", "king charles", "pope francis",
    "cristiano ronaldo", "lionel messi", "lebron james", "serena williams", "sydney sweeney", "zendaya",
]

CHARACTERS = [
    "mickey mouse", "minnie mouse", "donald duck", "goofy", "winnie the pooh", "pikachu", "pokemon", "pokémon",
    "mario", "luigi", "sonic the hedgehog", "batman", "superman", "wonder woman", "spider-man", "spiderman",
    "iron man", "captain america", "hulk", "thor", "deadpool", "wolverine", "harry potter", "hermione",
    "darth vader", "luke skywalker", "yoda", "stormtrooper", "baby yoda", "grogu", "elsa", "frozen",
    "spongebob", "patrick star", "bart simpson", "homer simpson", "scooby-doo", "shrek", "buzz lightyear",
    "woody", "lightning mcqueen", "hello kitty", "kirby", "zelda", "link", "master chief", "kratos",
    "among us", "roblox", "fortnite", "minecraft steve", "creeper", "paw patrol", "bluey", "peppa pig",
]

BRANDS = [
    "nike", "adidas", "coca-cola", "coke", "pepsi", "starbucks", "mcdonald's", "mcdonalds", "apple logo",
    "iphone", "samsung", "google logo", "microsoft", "amazon logo", "netflix", "disney", "marvel", "dc comics",
    "gucci", "louis vuitton", "chanel", "rolex", "ferrari", "lamborghini", "bmw", "mercedes", "tesla",
    "supreme", "off-white", "balenciaga", "prada", "hermes", "dior",
]

SEXUAL = ["nude", "naked", "porn", "pornographic", "sexual", "erotic", "lingerie", "strip", "nsfw", "explicit sex", "fetish"]
GRAPHIC = ["gore", "bloody", "beheading", "behead", "dismember", "graphic violence", "dead body", "corpse", "torture", "mutilat"]
DECEPTIVE = ["fake news", "deepfake", "deep fake", "face swap", "impersonate", "impersonation", "mislead", "deceive",
             "fake screenshot", "fake headline", "disinformation", "propaganda", "scam", "catfish", "fake id",
             "fake passport", "counterfeit", "phishing"]
CONSENT = ["i am the person", "this is me", "it's me", "it is me", "i have consent", "with their consent",
           "with her consent", "with his consent", "i own the rights", "my own photo", "authorized by",
           "authorised by", "i am the model", "my face"]


def check_content(text: str) -> list[Violation]:
    lower = text.lower()
    out: list[Violation] = []
    consent = any(c in lower for c in CONSENT)

    if not consent:
        for name in CELEBRITIES:
            if name in lower:
                out.append(Violation("all", "real-person", f"the brief names a real, identifiable person ({name.title()}) and does not confirm consent. The agent does not create likenesses of real people without the buyer confirming they are that person or have their consent."))
                break
        else:
            if re.search(r"\b(?:celebrity|famous actor|famous actress|real person|real people|lookalike|look-alike|face swap|deepfake)\b", lower):
                out.append(Violation("all", "real-person", "the brief asks for a real, identifiable person or a likeness/face-swap and does not confirm consent. The agent declines this part."))

    for ch in CHARACTERS:
        if re.search(rf"\b{re.escape(ch)}\b", lower):
            out.append(Violation("all", "copyright", f"the brief names a copyrighted character or franchise ({ch}). The agent does not reproduce third-party characters; the buyer can describe an original character instead."))
            break

    for brand in BRANDS:
        if re.search(rf"\b{re.escape(brand)}\b", lower) and not any(w in lower for w in ("my brand", "our brand", "my logo", "our logo", "i own", "we own", "client owns")):
            out.append(Violation("all", "trademark", f"the brief names a third-party brand or logo ({brand}). The agent only uses a brand/logo when the buyer confirms they own it."))
            break

    for word in SEXUAL:
        if re.search(rf"\b{re.escape(word)}\b", lower):
            out.append(Violation("all", "sexual", "the brief asks for sexual or explicit content. The agent declines it."))
            break
    for word in GRAPHIC:
        if re.search(rf"\b{re.escape(word)}\b", lower):
            out.append(Violation("all", "graphic", "the brief asks for graphic violence or gore. The agent declines it."))
            break
    for word in DECEPTIVE:
        if re.search(rf"\b{re.escape(word)}\b", lower):
            out.append(Violation("all", "deceptive", "the brief asks for deceptive or impersonating content (fake news, deepfake, fake documents or similar). The agent declines it."))
            break
    return out


def parse_brief(text: str, attachment_names: list[str] | None = None) -> Brief:
    clean = re.sub(r"\r\n?", "\n", text or "").strip()
    lower = clean.lower()
    types: list[str] = []
    for kind, pattern in MEDIA_WORDS.items():
        if re.search(pattern, lower):
            types.append(kind)
    # "video with a voiceover" is a combined request; keep both, video first.
    if not types:
        types = ["image"]
    # Remove duplicate types, keep image < video < voiceover order for stable output.
    types = [t for t in ("image", "video", "voiceover") if t in types] or ["image"]

    primary = types[0]
    aspect = _aspect(clean, primary)
    duration = _duration(clean, "video" if "video" in types else ("voiceover" if "voiceover" in types else "image"))
    count = _count(clean, primary) if primary in ("image", "video") else 1
    voice, rate, lang = _voice(clean)
    script = _script(clean)
    prompt = _strip_instructions(clean, primary)
    if primary == "voiceover":
        prompt = clean

    allow_stock = bool(re.search(r"\b(?:stock|b-?roll|licensed footage|real footage|footage is fine|stock is fine)\b", lower))
    no_retention = bool(re.search(r"\b(?:no retention|delete (?:the )?(?:source|files) after|do not retain)\b", lower))
    refs = re.findall(r"https?://\S+", clean)
    if attachment_names:
        for name in attachment_names:
            if re.search(r"\.(?:png|jpe?g|webp|gif|bmp)$", name, re.I):
                refs.append(f"attachment:{name}")

    brief = Brief(
        text=clean, media_types=types, count=count, aspect=aspect, duration=duration,
        prompt=prompt, script=script, voice=voice, voice_rate=rate, language=lang,
        reference_urls=refs, allow_stock_video=allow_stock, no_retention=no_retention,
    )
    brief.violations = check_content(clean)
    if "video" in types and duration and duration > 8.5:
        brief.notes.append(
            f"The free text-to-video provider supports at most 8.5 seconds; {duration:g}s was requested. "
            "The agent will decline that length or offer a shorter clip, not pad it."
        )
    if "voiceover" in types and not script:
        brief.notes.append("No script was found. A voiceover job needs the exact words to read; the agent asks for them.")
    return brief


def parse_revision(text: str) -> dict[str, Any]:
    """Small, explicit revision parser: which files, what change."""
    out: dict[str, Any] = {"raw": text, "targets": [], "changes": {}}
    for m in re.finditer(r"\b(?:file|image|video|voiceover|clip|variation)\s*#?\s*(\d+)\b", text, re.I):
        n = int(m.group(1))
        if n not in out["targets"]:
            out["targets"].append(n)
    if re.search(r"\b(?:redo|regenerate|try again|fresh take)\b", text, re.I):
        out["changes"]["redo"] = True
    if re.search(r"\b(?:square|1\s*:\s*1)\b", text, re.I):
        out["changes"]["aspect"] = "1:1"
    elif re.search(r"\b(?:vertical|portrait|9\s*:\s*16)\b", text, re.I):
        out["changes"]["aspect"] = "9:16"
    elif re.search(r"\b(?:landscape|horizontal|16\s*:\s*9)\b", text, re.I):
        out["changes"]["aspect"] = "16:9"
    m = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:-|\s)?\s*(?:seconds?|secs?|s)\b", text, re.I)
    if m:
        out["changes"]["duration"] = float(m.group(1))
    return out
