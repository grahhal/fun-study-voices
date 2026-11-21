"""Plain helpers for the Fun Study voice worker (no network), tested in tests/test_voices.py.

Clip naming must match the app (app/src/core/clips.ts): tidy the sentence (drop direction marks, squeeze spaces),
then the first 20 hex of SHA-256 of its UTF-8 bytes. Clips live at <VOICE_VERSION>/<lang>/<name>.mp3.
"""
from __future__ import annotations

import hashlib
import re
from xml.sax.saxutils import escape

VOICE_VERSION = "v1"  # must match app/src/core/clips.ts

BIDI_MARKS = re.compile("[‎‏‪-‮⁦-⁩]")

# English: Kokoro "af_heart", made at speed 0.8, then played about 12% higher to sound younger (owner's pick).
EN_VOICE, EN_SPEED, EN_PITCH = "af_heart", 0.8, (25, 28)
# Arabic: Azure Zariyah, a little slower and 15% higher (owner's pick).
AZURE_AR_VOICE, AZURE_AR_RATE, AZURE_AR_PITCH = "ar-SA-ZariyahNeural", "-10%", "+15%"
AZURE_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
MP3_KBPS = 48


def clip_key(text: str) -> str:
    return " ".join(BIDI_MARKS.sub("", text).split())


def clip_name(text: str) -> str:
    return hashlib.sha256(clip_key(text).encode("utf-8")).hexdigest()[:20]


def clip_path(lang: str, text: str) -> str:
    """Where the clip is stored on R2 (also its key in the voice_clips table)."""
    return f"{VOICE_VERSION}/{lang}/{clip_name(text)}.mp3"


def azure_ssml(text: str) -> str:
    """The request body for Azure: the sentence, escaped, in Zariyah's voice with the owner's settings."""
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="ar-SA">'
        f'<voice name="{AZURE_AR_VOICE}"><prosody rate="{AZURE_AR_RATE}" pitch="{AZURE_AR_PITCH}">'
        f"{escape(clip_key(text))}</prosody></voice></speak>"
    )


def azure_budget_left(used_this_month: int, monthly_limit: int, safety: float = 0.98) -> int:
    """Characters Azure may still record this month (kept just under the free limit)."""
    return max(0, int(monthly_limit * safety) - used_this_month)


def plan_jobs(jobs: list[dict], recorded: set[str], azure_left: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Split queued sentences into: already recorded (just delete), to record now, and to keep for later
    (Arabic past this month's free Azure characters)."""
    done, now, later = [], [], []
    budget = azure_left
    for job in jobs:
        path = clip_path(job["lang"], job["text"])
        if path in recorded:
            done.append(job)
        elif job["lang"] == "ar":
            chars = len(clip_key(job["text"]))
            if chars <= budget:
                budget -= chars
                now.append(job)
            else:
                later.append(job)
        else:
            now.append(job)
    return done, now, later
