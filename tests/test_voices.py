import json
from pathlib import Path

from voices import VOICE_VERSION, azure_budget_left, azure_ssml, clip_key, clip_name, clip_path, plan_jobs, speakable

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "clip_names.json").read_text(encoding="utf-8"))


def test_clip_names_match_the_app():
    for raw, key, name in FIXTURE["cases"]:
        assert clip_key(raw) == key
        assert clip_name(raw) == name


def test_clip_path_has_version_language_and_name():
    assert clip_path("en", "Cow") == f"{VOICE_VERSION}/en/{clip_name('Cow')}.mp3"


def test_azure_ssml_escapes_and_uses_zariyah_settings():
    ssml = azure_ssml("أحسنت <3 & شكرا")
    assert "ar-SA-ZariyahNeural" in ssml
    assert 'rate="-10%"' in ssml and 'pitch="+15%"' in ssml
    assert "&lt;3 &amp;" in ssml


def test_budget_stays_under_the_free_limit():
    assert azure_budget_left(0, 500_000) == 490_000
    assert azure_budget_left(495_000, 500_000) == 0


def test_plan_skips_recorded_and_holds_arabic_past_the_budget():
    jobs = [
        {"id": 1, "lang": "en", "text": "Cow"},
        {"id": 2, "lang": "en", "text": "Hen"},
        {"id": 3, "lang": "ar", "text": "بقرة"},
        {"id": 4, "lang": "ar", "text": "دجاجة"},
    ]
    done, now, later = plan_jobs(jobs, {clip_path("en", "Hen")}, azure_left=5)
    assert [j["id"] for j in done] == [2]
    assert [j["id"] for j in now] == [1, 3]  # "بقرة" is 4 characters, within 5
    assert [j["id"] for j in later] == [4]  # "دجاجة" (5) no longer fits


SPEAKABLE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "speakable.json").read_text(encoding="utf-8"))


def test_math_is_read_aloud_as_words_like_the_app():
    for lang, written, spoken in SPEAKABLE["cases"]:
        assert speakable(written, lang) == spoken, (lang, written)


def test_math_clips_are_named_by_what_is_said():
    assert clip_path("en", "3+4=") == clip_path("en", "3 plus 4 equals")
    assert clip_path("en", "Cow") == f"{VOICE_VERSION}/en/{clip_name('Cow')}.mp3"
