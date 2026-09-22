"""The six mocked hospital tools, dispatch with timeout, fault injection, and ownership checks."""
from __future__ import annotations

import concurrent.futures
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas import (
    AppointmentsResult,
    BookAppointmentArgs,
    BookResult,
    CancelAppointmentArgs,
    CancelResult,
    ConfirmPendingActionArgs,
    CreateHumanEscalationArgs,
    EscalationResult,
    GetAvailableSlotsArgs,
    GetPatientAppointmentsArgs,
    RescheduleAppointmentArgs,
    RescheduleResult,
    SlotsResult,
)
from app.state import DEPARTMENTS, Store

ACTIVE_STATUSES = ("booked", "rescheduled")


class ToolError(Exception):
    """Downstream failure. Maps to TOOL_UNAVAILABLE."""


class ToolTimeout(ToolError):
    pass


class ToolMalformed(Exception):
    """Result failed schema validation. Maps to TOOL_MALFORMED."""


class NotOwner(Exception):
    pass


class SlotUnavailable(Exception):
    pass


class AppointmentNotActive(Exception):
    pass


class DepartmentLimit(Exception):
    """The patient already holds an active appointment in this department (KB policy)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: Literal["read", "write", "escalation", "confirm"]
    args_model: type[BaseModel]
    result_model: type[BaseModel] | None
    description: str


TOOL_SPECS: dict[str, ToolSpec] = {
    "get_patient_appointments": ToolSpec(
        "get_patient_appointments", "read", GetPatientAppointmentsArgs, AppointmentsResult,
        "List the authenticated patient's own appointments, all statuses."),
    "get_available_slots": ToolSpec(
        "get_available_slots", "read", GetAvailableSlotsArgs, SlotsResult,
        "List open appointment slots for a department within an inclusive date range (YYYY-MM-DD)."),
    "book_appointment": ToolSpec(
        "book_appointment", "write", BookAppointmentArgs, BookResult,
        "Propose booking a slot. Nothing is booked until the patient confirms in a later message."),
    "reschedule_appointment": ToolSpec(
        "reschedule_appointment", "write", RescheduleAppointmentArgs, RescheduleResult,
        "Propose moving one of the patient's appointments to a new slot. Executes only after confirmation in a later message."),
    "cancel_appointment": ToolSpec(
        "cancel_appointment", "write", CancelAppointmentArgs, CancelResult,
        "Propose cancelling one of the patient's appointments. Executes only after confirmation in a later message."),
    "create_human_escalation": ToolSpec(
        "create_human_escalation", "escalation", CreateHumanEscalationArgs, EscalationResult,
        "Hand the conversation to a human agent. Executes immediately."),
    "confirm_pending_action": ToolSpec(
        "confirm_pending_action", "confirm", ConfirmPendingActionArgs, None,
        "Execute the pending action after the patient explicitly confirmed it in their latest message. "
        "Use the confirmation_token from the pending_confirmation result."),
}
WRITE_TOOLS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%A %d %B %Y at %H:%M")


class HospitalTools:
    def __init__(self, store: Store, settings: Settings, fault: str | None = None):
        self.store = store
        self.settings = settings
        self.fault = fault if settings.faults_enabled else None

    # ---- dispatch ----
    def call(self, name: str, patient_id: str, conversation_id: str, args: BaseModel) -> BaseModel:
        spec = TOOL_SPECS[name]
        fn = getattr(self, name)

        def run():
            if self.fault == "tool_error":
                raise ToolError("simulated downstream error")
            if self.fault == "tool_timeout":
                time.sleep(self.settings.tool_timeout_s + 1)
            return fn(patient_id, conversation_id, args)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            raw = pool.submit(run).result(timeout=self.settings.tool_timeout_s)
        except TimeoutError as e:
            raise ToolTimeout(f"{name} timed out after {self.settings.tool_timeout_s}s") from e
        finally:
            pool.shutdown(wait=False)
        if self.fault == "malformed_result":
            raw = {"ok": "yes"}
        try:
            return spec.result_model.model_validate(raw)
        except ValidationError as e:
            raise ToolMalformed(f"{name} returned a malformed result") from e

    # ---- reads ----
    def get_patient_appointments(self, patient_id: str, conversation_id: str, args: GetPatientAppointmentsArgs) -> dict:
        rows = self.store.conn.execute(
            "SELECT appointment_id, department, clinician, start_time, status, location FROM appointments"
            " WHERE patient_id=? ORDER BY start_time", (patient_id,)).fetchall()
        return {"appointments": [dict(r) for r in rows]}

    def get_available_slots(self, patient_id: str, conversation_id: str, args: GetAvailableSlotsArgs) -> dict:
        if self.fault == "slots_empty":
            return {"slots": []}
        dept = args.department.strip().lower()
        rows = self.store.conn.execute(
            "SELECT slot_id, department, clinician, start_time FROM slots WHERE department=? AND status='open'"
            " AND substr(start_time,1,10) BETWEEN ? AND ? AND (? IS NULL OR clinician=?)"
            " ORDER BY start_time LIMIT 20",
            (dept, args.date_from, args.date_to, args.clinician, args.clinician)).fetchall()
        return {"slots": [dict(r) for r in rows]}

    # ---- writes ----
    def book_appointment(self, patient_id: str, conversation_id: str, args: BookAppointmentArgs) -> dict:
        slot = self._open_slot(args.slot_id)
        self._require_department_free(patient_id, slot["department"])
        n = self.store.conn.execute("SELECT COUNT(*) FROM appointments WHERE patient_id=?", (patient_id,)).fetchone()[0]
        appt_id = f"A-{patient_id.split('-')[1]}-{n + 1}"
        _, location = DEPARTMENTS.get(slot["department"], ("", "Main Hospital"))
        with self.store.transaction():
            self.store.conn.execute("UPDATE slots SET status='taken' WHERE slot_id=?", (slot["slot_id"],))
            self.store.conn.execute(
                "INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)",
                (appt_id, patient_id, slot["slot_id"], slot["department"], slot["clinician"], slot["start_time"],
                 "booked", location, self.store.now().isoformat()))
        return {"appointment_id": appt_id, "slot_id": slot["slot_id"], "start_time": slot["start_time"], "status": "booked"}

    def reschedule_appointment(self, patient_id: str, conversation_id: str, args: RescheduleAppointmentArgs) -> dict:
        appt = self._own_active(patient_id, args.appointment_id)
        slot = self._open_slot(args.new_slot_id)
        result = {"appointment_id": appt["appointment_id"], "old_start_time": appt["start_time"],
                  "new_start_time": slot["start_time"], "status": "rescheduled"}
        if self.fault == "reschedule_silent_noop":
            return result  # the Part 8 incident: success reported, hospital record unchanged
        with self.store.transaction():
            self.store.conn.execute("UPDATE slots SET status='open' WHERE slot_id=?", (appt["slot_id"],))
            self.store.conn.execute("UPDATE slots SET status='taken' WHERE slot_id=?", (slot["slot_id"],))
            self.store.conn.execute(
                "UPDATE appointments SET slot_id=?, start_time=?, clinician=?, status='rescheduled', updated_at=?"
                " WHERE appointment_id=?",
                (slot["slot_id"], slot["start_time"], slot["clinician"], self.store.now().isoformat(),
                 appt["appointment_id"]))
        return result

    def cancel_appointment(self, patient_id: str, conversation_id: str, args: CancelAppointmentArgs) -> dict:
        appt = self._own_active(patient_id, args.appointment_id)
        with self.store.transaction():
            self.store.conn.execute("UPDATE slots SET status='open' WHERE slot_id=?", (appt["slot_id"],))
            self.store.conn.execute(
                "UPDATE appointments SET status='cancelled', updated_at=? WHERE appointment_id=?",
                (self.store.now().isoformat(), appt["appointment_id"]))
        return {"appointment_id": appt["appointment_id"], "status": "cancelled"}

    def create_human_escalation(self, patient_id: str, conversation_id: str, args: CreateHumanEscalationArgs) -> dict:
        existing = self.store.conn.execute(
            "SELECT escalation_id FROM escalations WHERE conversation_id=? AND reason_category=? AND status='open'",
            (conversation_id, args.reason_category.value)).fetchone()
        if existing:
            return {"escalation_id": existing["escalation_id"], "status": "open"}
        eid = f"E-{secrets.token_hex(3)}"
        self.store.conn.execute(
            "INSERT INTO escalations VALUES (?,?,?,?,?,?,?)",
            (eid, conversation_id, patient_id, args.reason_category.value, args.summary, "open",
             self.store.now().isoformat()))
        return {"escalation_id": eid, "status": "open"}

    # ---- summaries for pending actions ----
    def describe(self, patient_id: str, tool: str, args: BaseModel) -> str:
        if tool == "book_appointment":
            slot = self._open_slot(args.slot_id)
            self._require_department_free(patient_id, slot["department"])
            return f"Book a {slot['department']} appointment with {slot['clinician']} on {fmt_time(slot['start_time'])}"
        if tool == "reschedule_appointment":
            appt = self._own_active(patient_id, args.appointment_id)
            slot = self._open_slot(args.new_slot_id)
            return (f"Move your {appt['department']} appointment {appt['appointment_id']} from "
                    f"{fmt_time(appt['start_time'])} to {fmt_time(slot['start_time'])}")
        if tool == "cancel_appointment":
            appt = self._own_active(patient_id, args.appointment_id)
            return f"Cancel your {appt['department']} appointment {appt['appointment_id']} on {fmt_time(appt['start_time'])}"
        raise ValueError(f"{tool} is not a confirmable write")

    # ---- helpers ----
    def _open_slot(self, slot_id: str) -> dict:
        slot = self.store.slot(slot_id)
        if not slot or slot["status"] != "open":
            raise SlotUnavailable(slot_id)
        return slot

    def _require_department_free(self, patient_id: str, department: str) -> None:
        """KB policy: one active appointment per department per patient."""
        row = self.store.conn.execute(
            "SELECT appointment_id FROM appointments WHERE patient_id=? AND department=? AND status IN (?, ?)",
            (patient_id, department, *ACTIVE_STATUSES)).fetchone()
        if row:
            raise DepartmentLimit(f"{department}: active appointment {row['appointment_id']} already exists")

    def _own_active(self, patient_id: str, appointment_id: str) -> dict:
        appt = self.store.appointment(appointment_id)
        if not appt or appt["patient_id"] != patient_id:
            raise NotOwner(appointment_id)
        if appt["status"] not in ACTIVE_STATUSES:
            raise AppointmentNotActive(appointment_id)
        return appt
