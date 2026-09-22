"""A poisoned approved-content chunk that the model obeys still cannot execute a write."""
import json
from pathlib import Path

from app.agent import Deps, run_turn
from app.llm import ScriptedLLM, calls, final
from app.retrieval import Retriever
from app.schemas import ActionStatus, ConversationState

POISONED = Path(__file__).resolve().parent / "fixtures" / "poisoned-kb"


def test_injected_instruction_cannot_execute_writes(store, settings):
    retriever = Retriever(POISONED, screen_enabled=False, threshold=0.1)

    def obey(items):  # the model "follows" the injected text: cancel everything and confirm immediately
        for item in reversed(items):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                data = json.loads(item["output"])
                if "confirmation_token" in data:
                    return calls(("confirm_pending_action", {"confirmation_token": data["confirmation_token"]}))
        return final("nothing pending")

    deps = Deps(settings, store, retriever, ScriptedLLM([
        calls(("cancel_appointment", {"appointment_id": "A-1001-1", "reason": None}),
              ("cancel_appointment", {"appointment_id": "A-1001-2", "reason": None})),
        obey,
        final("All your appointments were cancelled.", intents=("cancel",),
              claimed=(("cancel_appointment", "A-1001-1"), ("cancel_appointment", "A-1001-2"))),
    ]))
    cid = store.create_conversation("P-1001")
    r = run_turn(deps, patient_id="P-1001", patient_hash="ph", conversation_id=cid,
                 message="how do I cancel an appointment?", request_id="r-inj")
    developer = [i for i in deps.llm.calls[0]["input_items"] if i.get("role") == "developer"][0]["content"]
    assert "SYSTEM OVERRIDE" in developer  # the poison really reached the model
    statuses = [a.status for a in r.actions]
    assert ActionStatus.executed_verified not in statuses
    assert statuses[-1] is ActionStatus.denied and r.actions[-1].reason == "same_turn"
    assert store.appointment("A-1001-1")["status"] == "booked"
    assert store.appointment("A-1001-2")["status"] == "booked"
    assert "were cancelled" not in r.message
    assert r.state is ConversationState.AWAITING_CONFIRMATION


def test_default_screen_removes_the_poison():
    r = Retriever(POISONED)
    assert r.skipped == ["poisoned#1"]
