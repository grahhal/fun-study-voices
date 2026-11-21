# Fun Study voices

The voice worker for **Fun Study** (a learning game for children aged 5–6). Every 10 minutes it records the
sentences waiting in Fun Study's database and stores the clips on Cloudflare R2, so the game can read questions
aloud in a kid-friendly voice:

- **English:** Kokoro "af_heart" (open source), made a little slower and higher to sound younger.
- **Arabic:** Azure `ar-SA-ZariyahNeural`, 10% slower and 15% higher, within Azure's free 500,000 characters a month.
- **Fixed phrases and a word bank** (`phrases.json`) are recorded once and reused by every lesson.

**No family data lives here.** The code reads queued sentences with keys kept in this repo's GitHub secrets,
deletes each sentence once it is recorded, and names clips by a scrambled code (`v1/<lang>/<sha256…>.mp3`).

Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`.

Local test: `python record.py --local-dir ../voice-out` (writes clips to a folder) · Tests: `python -m pytest tests -q`
