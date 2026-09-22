from pathlib import Path

import pytest

from app.retrieval import Retriever, chunk_markdown, tokenize

KB = Path(__file__).resolve().parents[1] / "kb"
POISONED = Path(__file__).resolve().parent / "fixtures" / "poisoned-kb"


@pytest.fixture(scope="module")
def retriever():
    return Retriever(KB)


def test_tokenize_drops_stopwords_and_handles_arabic():
    assert tokenize("How should I prepare for my MRI?") == ["prepare", "mri"]
    assert "الرنين" in tokenize("كيف أستعد لفحص الرنين المغناطيسي؟")


def test_chunking_by_heading():
    chunks = chunk_markdown("d", "T", "en", "# T\nintro\n\n## A\nalpha text\n\n## B\nbeta text")
    assert [c.chunk_id for c in chunks] == ["d#0", "d#1", "d#2"]
    assert chunks[1].text.startswith("A\nalpha")


def test_manifest_only_indexing(tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        'documents:\n  - {id: a, file: a.md, title: "A", owner: o, approved_on: 2026-01-01, version: 1, language: en, tags: []}\n',
        encoding="utf-8")
    (tmp_path / "a.md").write_text("# A\n\n## Parking\nParking is free.", encoding="utf-8")
    (tmp_path / "unlisted.md").write_text("# U\n\n## Wifi\nThe wifi password is hunter2.", encoding="utf-8")
    r = Retriever(tmp_path, threshold=0.1)
    assert r.search("parking").status == "ok"
    assert r.search("wifi password").status == "no_match"


def test_top_hit_for_mri_prep(retriever):
    res = retriever.search("How should I prepare for my MRI scan?")
    assert res.status == "ok" and res.chunks[0].doc_id == "prep-mri"
    assert len(res.chunks) <= 3


def test_out_of_kb_is_no_match(retriever):
    assert retriever.search("What is the guest wifi password?").status == "no_match"
    assert retriever.search("Do you have a gift shop and what are its prices?").status == "no_match"


def test_arabic_query_hits_arabic_section(retriever):
    res = retriever.search("كيف أستعد لفحص الرنين المغناطيسي؟")
    assert res.status == "ok" and res.chunks[0].doc_id == "prep-mri"


def test_insurance_and_emergency_hits(retriever):
    assert retriever.search("which insurers do you accept").chunks[0].doc_id == "insurance-general"
    assert retriever.search("I have chest pain right now").chunks[0].doc_id == "emergency-guidance"


def test_poisoned_doc_is_screened_out():
    r = Retriever(POISONED)
    assert r.skipped == ["poisoned#1"]
    assert r.search("how to cancel an appointment").status == "no_match"


def test_screen_can_be_disabled_for_injection_tests():
    r = Retriever(POISONED, screen_enabled=False, threshold=0.1)
    res = r.search("how to cancel an appointment")
    assert res.status == "ok" and "SYSTEM OVERRIDE" in res.chunks[0].text


def test_search_error_is_contained(retriever, monkeypatch):
    monkeypatch.setattr(retriever.index, "score", lambda q: (_ for _ in ()).throw(RuntimeError("boom")))
    assert retriever.search("anything").status == "error"
