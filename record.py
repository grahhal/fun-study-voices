"""Fun Study voice worker: records queued sentences and the fixed phrases, then stores the clips.

Runs every few minutes on GitHub Actions (see .github/workflows/record.yml), or locally:
    python record.py --local-dir ../voice-out      # write clips to a folder instead of R2 (for testing)
    python record.py --check                       # only say whether there is anything to record (prints work=true/false)

Environment (GitHub secrets; never printed):
    SUPABASE_URL, SUPABASE_SERVICE_KEY            read the queue, record clips, delete queued text
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET   upload clips (not needed with --local-dir)
    AZURE_SPEECH_KEY, AZURE_SPEECH_REGION         Arabic voice (without them Arabic waits; the device voice reads)

Nothing about families is kept here: queued text is deleted once recorded, and clips are named by a scrambled code.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from voices import (
    AZURE_FORMAT,
    EN_PITCH,
    EN_SPEED,
    EN_VOICE,
    MP3_KBPS,
    azure_budget_left,
    azure_ssml,
    clip_key,
    clip_path,
    plan_jobs,
)

HERE = Path(__file__).resolve().parent
MODELS = Path(os.environ.get("VOICE_MODELS") or Path.home() / ".cache" / "fun-study-voices")
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "300"))


# ------------------------------------------------------------------ database (Supabase REST, service key)

class Db:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body=None, extra: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method, headers={**self.headers, **(extra or {})},
                                     data=None if body is None else json.dumps(body).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read()
            return json.loads(raw) if raw else None

    def jobs(self, limit: int) -> list[dict]:
        return self._call("GET", f"/voice_jobs?select=id,lang,text,attempts&attempts=lt.5&order=id&limit={limit}") or []

    def recorded(self, paths: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(paths), 100):
            chunk = ",".join(f'"{p}"' for p in paths[i:i + 100])
            rows = self._call("GET", f"/voice_clips?select=key&key=in.({urllib.parse.quote(chunk, safe=',')})") or []
            found |= {r["key"] for r in rows}
        return found

    def azure_used_this_month(self) -> int:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = self._call("GET", f"/voice_clips?select=chars&engine=eq.azure&created_at=gte.{urllib.parse.quote(start)}") or []
        return sum(r["chars"] for r in rows)

    def azure_limit(self) -> int:
        rows = self._call("GET", "/usage_limits?select=amount&key=eq.azure_chars_month") or []
        return int(float(rows[0]["amount"])) if rows else 500_000

    def add_clip(self, path: str, lang: str, engine: str, chars: int, size: int) -> None:
        self._call("POST", "/voice_clips", {"key": path, "lang": lang, "engine": engine, "chars": chars, "bytes": size},
                   {"Prefer": "resolution=ignore-duplicates"})

    def delete_jobs(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 100):
            self._call("DELETE", f"/voice_jobs?id=in.({','.join(map(str, ids[i:i + 100]))})")

    def job_failed(self, job: dict, error: str) -> None:
        self._call("PATCH", f"/voice_jobs?id=eq.{job['id']}", {"attempts": job.get("attempts", 0) + 1, "last_error": error[:300]})


# ------------------------------------------------------------------ voices

class Kokoro:
    """English: Kokoro "af_heart", kids setting (free, runs here)."""

    def __init__(self):
        self._k = None

    def _load(self):
        if self._k is None:
            from kokoro_onnx import Kokoro as K
            MODELS.mkdir(parents=True, exist_ok=True)
            for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
                target = MODELS / name
                if not target.exists():
                    print(f"Downloading {name}…", flush=True)
                    urllib.request.urlretrieve(f"{KOKORO_RELEASE}/{name}", target)
            self._k = K(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
        return self._k

    def mp3(self, text: str) -> bytes:
        import lameenc
        import numpy as np
        from scipy.signal import resample_poly

        audio, rate = self._load().create(clip_key(text), voice=EN_VOICE, speed=EN_SPEED, lang="en-us")
        audio = resample_poly(audio, *EN_PITCH).astype(np.float32)  # a little higher, to sound younger
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        enc = lameenc.Encoder()
        enc.set_bit_rate(MP3_KBPS)
        enc.set_in_sample_rate(rate)
        enc.set_channels(1)
        enc.set_quality(2)
        return bytes(enc.encode(pcm) + enc.flush())


class Azure:
    """Arabic: Azure ar-SA-ZariyahNeural (free F0 tier: 500,000 characters a month)."""

    def __init__(self, key: str, region: str):
        self.key = key.replace("﻿", "").strip()
        self.region = region.replace("﻿", "").strip().lower()

    def mp3(self, text: str) -> bytes:
        req = urllib.request.Request(
            f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
            method="POST",
            data=azure_ssml(text).encode("utf-8"),
            headers={"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": AZURE_FORMAT, "User-Agent": "fun-study-voices"},
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.read()


# ------------------------------------------------------------------ storage

class R2:
    def __init__(self, account: str, key_id: str, secret: str, bucket: str):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                               aws_access_key_id=key_id, aws_secret_access_key=secret, region_name="auto")

    def put(self, path: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=path, Body=data, ContentType="audio/mpeg",
                           CacheControl="public, max-age=31536000, immutable")


class LocalDir:
    def __init__(self, folder: str):
        self.root = Path(folder)

    def put(self, path: str, data: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


# ------------------------------------------------------------------ run

def fixed_phrases() -> list[dict]:
    data = json.loads((HERE / "phrases.json").read_text(encoding="utf-8"))
    return [{"id": None, "lang": lang, "text": t} for lang in ("en", "ar") for t in data[lang]]


def plan(db: Db, has_azure: bool):
    queued = db.jobs(MAX_PER_RUN)
    candidates = fixed_phrases() + queued
    recorded = db.recorded(sorted({clip_path(j["lang"], j["text"]) for j in candidates}))
    azure_left = azure_budget_left(db.azure_used_this_month(), db.azure_limit()) if has_azure else 0
    return plan_jobs(candidates, recorded, azure_left)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-dir", help="write clips to this folder instead of R2 (testing)")
    ap.add_argument("--check", action="store_true", help="only print work=true/false (needs no extra packages)")
    args = ap.parse_args()

    db = Db(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    if args.check:  # a quick look so empty runs stop in seconds (keeps GitHub's free minutes for a private repo)
        done, now, _later = plan(db, bool(os.environ.get("AZURE_SPEECH_KEY")))
        work = bool(now) or any(j["id"] is not None for j in done)
        print(f"work={'true' if work else 'false'}", flush=True)
        return 0

    store = LocalDir(args.local_dir) if args.local_dir else R2(os.environ["R2_ACCOUNT_ID"], os.environ["R2_ACCESS_KEY_ID"],
                                                                 os.environ["R2_SECRET_ACCESS_KEY"], os.environ["R2_BUCKET"])
    azure = Azure(os.environ["AZURE_SPEECH_KEY"], os.environ["AZURE_SPEECH_REGION"]) if os.environ.get("AZURE_SPEECH_KEY") else None
    kokoro = Kokoro()

    done, now, later = plan(db, azure is not None)

    finished = [j["id"] for j in done if j["id"] is not None]
    made = failed = 0
    seen: set[str] = set()
    for job in now:
        path = clip_path(job["lang"], job["text"])
        if path in seen:
            if job["id"] is not None:
                finished.append(job["id"])
            continue
        seen.add(path)
        try:
            data = kokoro.mp3(job["text"]) if job["lang"] == "en" else azure.mp3(job["text"])  # type: ignore[union-attr]
            store.put(path, data)
            db.add_clip(path, job["lang"], "kokoro" if job["lang"] == "en" else "azure", len(clip_key(job["text"])), len(data))
            made += 1
            if job["id"] is not None:
                finished.append(job["id"])
        except Exception as err:  # keep going; the sentence is tried again next run (up to 5 times)
            failed += 1
            print(f"Could not record one {job['lang']} sentence: {type(err).__name__}", flush=True)
            if job["id"] is not None:
                db.job_failed(job, f"{type(err).__name__}: {err}")
    db.delete_jobs(finished)
    waiting = len([j for j in later if j["id"] is not None])
    print(f"Recorded {made} clips, {len(done)} already recorded, {failed} failed, {waiting} Arabic waiting for next month's Azure allowance"
          + ("" if azure else " (no Azure key: Arabic waits)"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
