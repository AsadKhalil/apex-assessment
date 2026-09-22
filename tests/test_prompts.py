from dataclasses import dataclass

from app.prompts import (
    TEMPLATE_KEYS,
    TEMPLATES,
    no_retrieved_note,
    pending_note,
    system_instructions,
    t,
    verified_success,
    wrap_retrieved,
)


@dataclass
class Chunk:
    chunk_id: str
    title: str
    text: str


def test_every_template_exists_in_both_languages():
    for lang in ("en", "ar"):
        assert set(TEMPLATES[lang]) == set(TEMPLATE_KEYS)


def test_templates_render():
    en = t("confirmation", "en", summary="Cancel A-1001-1", expires="09:10")
    assert "Cancel A-1001-1" in en and '"yes"' in en
    ar = t("confirmation", "ar", summary="Cancel A-1001-1", expires="09:10")
    assert "نعم" in ar
    assert "E-abc" in t("unverified", "en", escalation_id="E-abc")
    assert t("no_info", "fr") == t("no_info", "en")  # unknown language falls back to English


def test_verified_success_uses_verified_facts():
    msg = verified_success("reschedule_appointment",
                           {"appointment_id": "A-1001-2", "new_start_time": "2026-10-08T14:00:00+03:00"},
                           "dermatology", "en")
    assert "A-1001-2" in msg and "Thursday 08 October 2026 at 14:00" in msg
    booked = verified_success("book_appointment",
                              {"appointment_id": "A-1001-3", "start_time": "2026-10-13T09:00:00+03:00"},
                              "cardiology", "ar")
    assert "A-1001-3" in booked and "cardiology" in booked
    assert "A-1001-1" in verified_success("cancel_appointment", {"appointment_id": "A-1001-1"}, "cardiology", "en")


def test_instructions_and_notes():
    s = system_instructions("2026-10-05")
    for needle in ("2026-10-05", "confirm_pending_action", "verified", "insurance", "997", "Sunday to Thursday"):
        assert needle in s
    wrapped = wrap_retrieved([Chunk("prep-mri#1", "MRI preparation", "Remove metal objects.")])
    assert '<approved_content source="prep-mri#1"' in wrapped and "Remove metal objects." in wrapped
    assert "not available" in no_retrieved_note()
    note = pending_note({"summary": "Cancel A-1001-1", "token": "tok123", "expires_at": "2026-10-05T09:10:00+03:00"})
    assert "tok123" in note and "Cancel A-1001-1" in note
