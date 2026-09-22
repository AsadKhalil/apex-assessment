from pathlib import Path

from evals.run import check, load_cases, run_case, write_report


def test_cases_file_is_well_formed():
    cases = load_cases()
    assert len(cases) >= 18
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["turns"] and c["modes"]
        if "fake" in c["modes"]:
            assert all("fake_llm" in t for t in c["turns"]), c["id"]


def test_every_fake_case_passes_in_fake_mode():
    failures = []
    for case in load_cases():
        if "fake" not in case["modes"]:
            continue
        result = run_case(case, "fake")
        if not result.passed:
            failures.append((case["id"], [t.problems for t in result.turns]))
    assert failures == []


def test_check_reports_each_unmet_expectation(store, settings, retriever):
    from app.agent import Deps, run_turn
    from app.llm import ScriptedLLM, final
    deps = Deps(settings, store, retriever, ScriptedLLM([final("hi")]))
    cid = store.create_conversation("P-1001")
    resp = run_turn(deps, patient_id="P-1001", patient_hash="ph", conversation_id=cid, message="hi", request_id="r")
    problems = check({"tools_called": ["book_appointment"], "state": "ESCALATED", "must_include_any": ["zzz"],
                      "appointment_status": {"A-1001-1": "cancelled"}}, resp, store)
    assert len(problems) == 4


def test_denied_reason_prefix_matches():
    from evals.run import reason_matches
    assert reason_matches("department_limit", {"department_limit: cardiology: active appointment A-1001-1"})
    assert reason_matches("not_owner", {"not_owner"})
    assert not reason_matches("not_owner", {"department_limit: x"})


def test_report_is_written(tmp_path):
    case = next(c for c in load_cases() if c["id"] == "hp_my_appointments")
    result = run_case(case, "fake")
    path = write_report([result], "fake", None, tmp_path / "r.md")
    text = Path(path).read_text(encoding="utf-8")
    assert "hp_my_appointments" in text and "| PASS |" in text and "Pass rate" in text
