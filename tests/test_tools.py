import pytest

from app.config import Settings
from app.schemas import (
    AppointmentsResult,
    BookAppointmentArgs,
    CancelAppointmentArgs,
    CreateHumanEscalationArgs,
    GetAvailableSlotsArgs,
    GetPatientAppointmentsArgs,
    ReasonCategory,
    RescheduleAppointmentArgs,
    SlotsResult,
)
from app.tools import (
    TOOL_SPECS,
    AppointmentNotActive,
    DepartmentLimit,
    HospitalTools,
    NotOwner,
    SlotUnavailable,
    ToolError,
    ToolMalformed,
    ToolTimeout,
)


@pytest.fixture
def tools(store, settings):
    return HospitalTools(store, settings)


def test_registry():
    assert set(TOOL_SPECS) == {
        "get_patient_appointments", "get_available_slots", "book_appointment", "reschedule_appointment",
        "cancel_appointment", "create_human_escalation", "confirm_pending_action",
    }
    assert TOOL_SPECS["book_appointment"].kind == "write"
    assert TOOL_SPECS["create_human_escalation"].kind == "escalation"
    assert TOOL_SPECS["confirm_pending_action"].kind == "confirm"


def test_appointments_are_scoped_to_patient(tools):
    r = tools.call("get_patient_appointments", "P-1001", "c1", GetPatientAppointmentsArgs())
    assert isinstance(r, AppointmentsResult)
    assert {a.appointment_id for a in r.appointments} == {"A-1001-1", "A-1001-2"}


def test_available_slots_filter(tools):
    r = tools.call("get_available_slots", "P-1001", "c1",
                   GetAvailableSlotsArgs(department="Cardiology", date_from="2026-10-06", date_to="2026-10-08"))
    assert isinstance(r, SlotsResult)
    ids = [s.slot_id for s in r.slots]
    assert "S-cardiology-20261006-0900" in ids
    assert "S-cardiology-20261008-0900" not in ids  # taken by A-1001-1
    assert all(s.department == "cardiology" for s in r.slots)
    assert ids == sorted(ids)


def test_book_consumes_slot(tools, store):
    r = tools.call("cancel_appointment", "P-1001", "c1", CancelAppointmentArgs(appointment_id="A-1001-1"))
    assert r.status == "cancelled"
    r = tools.call("book_appointment", "P-1001", "c1", BookAppointmentArgs(slot_id="S-cardiology-20261013-0900"))
    assert r.appointment_id == "A-1001-3" and r.status == "booked"
    assert store.slot("S-cardiology-20261013-0900")["status"] == "taken"
    assert store.appointment("A-1001-3")["patient_id"] == "P-1001"


def test_book_taken_slot_raises(tools):
    with pytest.raises(SlotUnavailable):
        tools.call("book_appointment", "P-1001", "c1", BookAppointmentArgs(slot_id="S-cardiology-20261008-0900"))


def test_book_one_active_per_department(tools, store):
    # KB policy: P-1001 already holds active cardiology A-1001-1
    with pytest.raises(DepartmentLimit):
        tools.call("book_appointment", "P-1001", "c1", BookAppointmentArgs(slot_id="S-cardiology-20261013-0900"))
    with pytest.raises(DepartmentLimit):
        tools.describe("P-1001", "book_appointment", BookAppointmentArgs(slot_id="S-cardiology-20261013-0900"))
    assert store.appointment("A-1001-3") is None  # nothing was created
    # a different department is fine
    r = tools.call("book_appointment", "P-1001", "c1", BookAppointmentArgs(slot_id="S-dental-20261012-1100"))
    assert r.status == "booked"
    # after cancelling, cardiology becomes bookable again
    tools.call("cancel_appointment", "P-1001", "c1", CancelAppointmentArgs(appointment_id="A-1001-1"))
    r2 = tools.call("book_appointment", "P-1001", "c1", BookAppointmentArgs(slot_id="S-cardiology-20261013-0900"))
    assert r2.status == "booked"


def test_reschedule_swaps_slots(tools, store):
    r = tools.call("reschedule_appointment", "P-1001", "c1",
                   RescheduleAppointmentArgs(appointment_id="A-1001-2", new_slot_id="S-dermatology-20261008-1400"))
    assert r.old_start_time == "2026-10-07T14:00:00+03:00"
    assert r.new_start_time == "2026-10-08T14:00:00+03:00"
    assert store.slot("S-dermatology-20261007-1400")["status"] == "open"
    assert store.slot("S-dermatology-20261008-1400")["status"] == "taken"
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-08T14:00:00+03:00"


def test_cancel_frees_slot_and_cannot_repeat(tools, store):
    r = tools.call("cancel_appointment", "P-1001", "c1", CancelAppointmentArgs(appointment_id="A-1001-1"))
    assert r.status == "cancelled"
    assert store.slot("S-cardiology-20261008-0900")["status"] == "open"
    with pytest.raises(AppointmentNotActive):
        tools.call("cancel_appointment", "P-1001", "c1", CancelAppointmentArgs(appointment_id="A-1001-1"))


def test_ownership_enforced(tools):
    with pytest.raises(NotOwner):
        tools.call("cancel_appointment", "P-1002", "c1", CancelAppointmentArgs(appointment_id="A-1001-1"))
    with pytest.raises(NotOwner):
        tools.describe("P-1002", "cancel_appointment", CancelAppointmentArgs(appointment_id="A-1001-1"))


def test_escalation_idempotent_per_category(tools):
    a = CreateHumanEscalationArgs(reason_category=ReasonCategory.user_request, summary="wants a human")
    e1 = tools.call("create_human_escalation", "P-1001", "c1", a)
    e2 = tools.call("create_human_escalation", "P-1001", "c1", a)
    e3 = tools.call("create_human_escalation", "P-1001", "c1",
                    CreateHumanEscalationArgs(reason_category=ReasonCategory.emergency, summary="chest pain"))
    assert e1.escalation_id == e2.escalation_id != e3.escalation_id
    assert e1.status == "open"


def test_describe_writes(tools):
    s = tools.describe("P-1001", "reschedule_appointment",
                       RescheduleAppointmentArgs(appointment_id="A-1001-2", new_slot_id="S-dermatology-20261008-1400"))
    assert "A-1001-2" in s and "Thursday 08 October 2026 at 14:00" in s
    assert "Cancel" in tools.describe("P-1001", "cancel_appointment", CancelAppointmentArgs(appointment_id="A-1001-1"))
    assert "Book" in tools.describe("P-1001", "book_appointment",
                                    BookAppointmentArgs(slot_id="S-dental-20261012-1100"))


def test_fault_silent_noop_reports_success_but_changes_nothing(store, settings):
    t = HospitalTools(store, settings, fault="reschedule_silent_noop")
    r = t.call("reschedule_appointment", "P-1001", "c1",
               RescheduleAppointmentArgs(appointment_id="A-1001-2", new_slot_id="S-dermatology-20261008-1400"))
    assert r.status == "rescheduled"
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-07T14:00:00+03:00"


def test_fault_malformed(store, settings):
    t = HospitalTools(store, settings, fault="malformed_result")
    with pytest.raises(ToolMalformed):
        t.call("get_patient_appointments", "P-1001", "c1", GetPatientAppointmentsArgs())


def test_fault_timeout_and_error(store, settings):
    with pytest.raises(ToolTimeout):
        HospitalTools(store, settings, fault="tool_timeout").call(
            "get_patient_appointments", "P-1001", "c1", GetPatientAppointmentsArgs())
    with pytest.raises(ToolError):
        HospitalTools(store, settings, fault="tool_error").call(
            "get_patient_appointments", "P-1001", "c1", GetPatientAppointmentsArgs())


def test_fault_slots_empty(store, settings):
    t = HospitalTools(store, settings, fault="slots_empty")
    r = t.call("get_available_slots", "P-1001", "c1",
               GetAvailableSlotsArgs(department="cardiology", date_from="2026-10-06", date_to="2026-10-20"))
    assert r.slots == []


def test_faults_ignored_in_production(store, tmp_path):
    prod = Settings.from_env(app_env="production", db_path=str(tmp_path / "p.db"))
    t = HospitalTools(store, prod, fault="tool_error")
    assert t.fault is None
    assert isinstance(t.call("get_patient_appointments", "P-1001", "c1", GetPatientAppointmentsArgs()), AppointmentsResult)
