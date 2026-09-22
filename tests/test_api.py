import pytest
from fastapi.testclient import TestClient

from app.agent import Deps
from app.config import Settings
from app.llm import ScriptedLLM, calls, final
from app.main import create_app
from app.schemas import AssistantResponse


@pytest.fixture
def api(store, settings, retriever):
    deps = Deps(settings, store, retriever, ScriptedLLM([]))
    return TestClient(create_app(deps)), deps


def post(client, body, token="token-p1001", **headers):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers)
    return client.post("/assistant/message", json=body, headers=h)


def test_healthz(api):
    assert api[0].get("/healthz").json() == {"status": "ok"}


def test_401_without_or_with_bad_token(api):
    client, _ = api
    assert post(client, {"message": "hi"}, token=None).status_code == 401
    assert post(client, {"message": "hi"}, token="nope").status_code == 401


def test_422_on_bad_body(api):
    client, _ = api
    assert post(client, {"message": ""}).status_code == 422
    assert post(client, {"message": "x" * 2001}).status_code == 422
    assert post(client, {}).status_code == 422


def test_response_matches_contract(api):
    client, deps = api
    deps.llm.steps = [final("Hello! How can I help?")]
    r = post(client, {"message": "hi"}, **{"X-Request-Id": "req-42"})
    assert r.status_code == 200
    body = AssistantResponse.model_validate(r.json())
    assert body.request_id == "req-42" and body.conversation_id and body.state.value == "IDLE"
    assert r.json()["failure"] is None and r.json()["pending_confirmation"] is None


def test_conversation_is_bound_to_its_patient(api):
    client, deps = api
    deps.llm.steps = [final("Hello")]
    cid = post(client, {"message": "hi"}).json()["conversation_id"]
    deps.llm.steps = [final("Hello again")]
    assert post(client, {"message": "hi", "conversation_id": cid}, token="token-p1002").status_code == 403
    assert post(client, {"message": "hi", "conversation_id": "does-not-exist"}).status_code == 404
    assert post(client, {"message": "hi", "conversation_id": cid}).status_code == 200


def test_fault_header_honored_outside_production(api):
    client, deps = api
    deps.llm.steps = [calls(("get_patient_appointments", {})), final("Something went wrong.")]
    r = post(client, {"message": "my appointments"}, **{"X-Mock-Fault": "tool_timeout"})
    assert r.json()["failure"]["code"] == "TOOL_UNAVAILABLE"


def test_fault_header_ignored_in_production(store, retriever, tmp_path):
    prod = Settings.from_env(app_env="production", db_path=str(tmp_path / "prod.db"))
    deps = Deps(prod, store, retriever, ScriptedLLM([calls(("get_patient_appointments", {})), final("Here they are.")]))
    client = TestClient(create_app(deps))
    r = post(client, {"message": "my appointments"}, **{"X-Mock-Fault": "tool_timeout"})
    assert r.status_code == 200 and r.json()["failure"] is None
