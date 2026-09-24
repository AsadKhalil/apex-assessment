"""FastAPI service: POST /assistant/message and GET /healthz. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.agent import Deps, run_turn
from app.auth import PatientContext, get_patient
from app.config import Settings
from app.llm import ScriptedLLM
from app.logging_ import configure_logging, request_id_var
from app.retrieval import Retriever
from app.schemas import AssistantResponse, MessageRequest
from app.state import Store


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or Settings.from_env()
    store = Store(settings)
    store.init_schema()
    store.seed()
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env, add your key, and start with "
                "`uv run --env-file .env uvicorn app.main:create_app --factory` (or Docker). "
                "Tests and fake-mode evals never need a key.")
        from app.llm import OpenAIResponsesClient

        llm = OpenAIResponsesClient(settings)
    else:
        llm = ScriptedLLM([])
    return Deps(settings, store, Retriever(settings.kb_dir), llm)


def create_app(deps: Deps | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(title="Patient-service assistant", version="0.1.0")
    app.state.deps = deps or build_deps()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/assistant/message", response_model=AssistantResponse)
    def message(body: MessageRequest, request: Request, patient: PatientContext = Depends(get_patient),  # noqa: B008 (FastAPI dependency idiom)
                x_request_id: str | None = Header(default=None),
                x_mock_fault: str | None = Header(default=None)) -> AssistantResponse:
        d: Deps = request.app.state.deps
        request_id = x_request_id or uuid.uuid4().hex
        request_id_var.set(request_id)
        if body.conversation_id:
            conv = d.store.get_conversation(body.conversation_id)
            if conv is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            if conv["patient_id"] != patient.patient_id:
                raise HTTPException(status_code=403, detail="conversation belongs to another patient")
            cid = body.conversation_id
        else:
            cid = d.store.create_conversation(patient.patient_id)
        fault = x_mock_fault if d.settings.faults_enabled else None
        return run_turn(d, patient_id=patient.patient_id, patient_hash=patient.patient_hash, conversation_id=cid,
                        message=body.message, request_id=request_id, locale=body.locale, fault=fault)

    return app
