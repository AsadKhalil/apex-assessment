import json
import logging

from app.logging_ import configure_logging, log_event, patient_hash, request_id_var


def last_line(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_log_event_redacts_sensitive_keys(capsys):
    configure_logging()
    log_event("turn_completed", message="secret text", patient_id="P-1001", content="hello", tool="x", latency_ms=5)
    line = last_line(capsys)
    assert line["event"] == "turn_completed" and line["tool"] == "x" and line["latency_ms"] == 5
    assert "secret text" not in json.dumps(line) and "P-1001" not in json.dumps(line)
    assert set(line["redacted"]) == {"message", "patient_id", "content"}


def test_extra_on_plain_logger_is_also_redacted(capsys):
    configure_logging()
    logging.getLogger("assistant").info("x", extra={"patient_id": "P-1001", "state": "IDLE"})
    line = last_line(capsys)
    assert line["state"] == "IDLE" and "P-1001" not in json.dumps(line)


def test_request_id_from_context(capsys):
    configure_logging()
    request_id_var.set("req-1")
    log_event("ping")
    assert last_line(capsys)["request_id"] == "req-1"


def test_patient_hash_is_keyed_and_stable():
    a = patient_hash("k1", "P-1001")
    assert a == patient_hash("k1", "P-1001") and len(a) == 16
    assert a != patient_hash("k2", "P-1001") and a != patient_hash("k1", "P-1002")
