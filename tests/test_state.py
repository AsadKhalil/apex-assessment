from app.config import Settings
from app.state import Store


def make_store(tmp_path, name="a.db"):
    s = Store(Settings.from_env(db_path=str(tmp_path / name)))
    s.init_schema()
    s.seed()
    return s


def test_seed_identity_and_appointments(tmp_path):
    s = make_store(tmp_path)
    assert s.patient_for_token("token-p1001") == "P-1001"
    assert s.patient_for_token("token-p1002") == "P-1002"
    assert s.patient_for_token("nope") is None
    a = s.appointment("A-1001-1")
    assert a["patient_id"] == "P-1001" and a["department"] == "cardiology"
    assert a["start_time"] == "2026-10-08T09:00:00+03:00" and a["status"] == "booked"
    assert s.appointment("A-1001-2")["start_time"] == "2026-10-07T14:00:00+03:00"
    assert s.appointment("A-1002-2")["start_time"] == "2026-10-11T09:00:00+03:00"


def test_seed_slots_follow_saudi_work_week(tmp_path):
    s = make_store(tmp_path)
    assert s.slot("S-cardiology-20261013-0900")["status"] == "open"
    assert s.slot("S-cardiology-20261008-0900")["status"] == "taken"  # A-1001-1
    assert s.slot("S-cardiology-20261009-0900") is None  # Friday
    assert s.slot("S-cardiology-20261010-0900") is None  # Saturday


def test_seed_is_idempotent(tmp_path):
    s = make_store(tmp_path)
    s.seed()
    assert s.conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2


def test_conversation_lifecycle(tmp_path):
    s = make_store(tmp_path)
    cid = s.create_conversation("P-1001")
    c = s.get_conversation(cid)
    assert c["patient_id"] == "P-1001" and c["state"] == "IDLE" and c["turn_index"] == 0
    assert s.next_turn(cid) == 1 and s.next_turn(cid) == 2
    s.set_state(cid, "ESCALATED")
    assert s.get_conversation(cid)["state"] == "ESCALATED"
    assert s.get_conversation("missing") is None


def test_messages_in_order(tmp_path):
    s = make_store(tmp_path)
    cid = s.create_conversation("P-1001")
    s.append_message(cid, "user", "hello")
    s.append_message(cid, "assistant", "hi")
    assert [m["role"] for m in s.list_messages(cid)] == ["user", "assistant"]


def test_pending_replace_and_close(tmp_path):
    s = make_store(tmp_path)
    cid = s.create_conversation("P-1001")
    s.set_pending(cid, token="t1", tool="cancel_appointment", args={"appointment_id": "A-1001-1"},
                  args_hash="h1", summary="cancel A-1001-1", turn_created=1, expires_at="2026-10-05T09:10:00+03:00")
    assert s.get_pending(cid)["token"] == "t1"
    assert s.get_pending(cid)["args"] == {"appointment_id": "A-1001-1"}
    s.set_pending(cid, token="t2", tool="cancel_appointment", args={"appointment_id": "A-1001-2"},
                  args_hash="h2", summary="cancel A-1001-2", turn_created=2, expires_at="2026-10-05T09:12:00+03:00")
    assert s.get_pending(cid)["token"] == "t2"
    row = s.conn.execute("SELECT status FROM pending_actions WHERE token='t1'").fetchone()
    assert row["status"] == "replaced"
    s.close_pending(cid, "used")
    assert s.get_pending(cid) is None


def test_ledger_and_audit(tmp_path):
    s = make_store(tmp_path)
    cid = s.create_conversation("P-1001")
    s.ledger_put("k1", cid, "cancel_appointment", "h1", {"appointment_id": "A-1001-1", "status": "cancelled"}, True)
    entry = s.ledger_get("k1")
    assert entry["result"]["status"] == "cancelled" and entry["verified"] is True
    assert s.ledger_get("k2") is None
    s.audit(request_id="r1", conversation_id=cid, patient_hash="ph", event="tool_call", tool="cancel_appointment", decision="ALLOW")
    events = s.list_audit(cid)
    assert events[0]["event"] == "tool_call" and events[0]["detail"]["decision"] == "ALLOW"
