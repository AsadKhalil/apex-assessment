"""Bearer token -> authenticated patient. A mock identity table stands in for the hospital IdP."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.logging_ import patient_hash


@dataclass(frozen=True)
class PatientContext:
    patient_id: str
    patient_hash: str


def get_patient(request: Request) -> PatientContext:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    deps = request.app.state.deps
    patient_id = deps.store.patient_for_token(token) if scheme.lower() == "bearer" and token else None
    if not patient_id:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return PatientContext(patient_id, patient_hash(deps.settings.log_hmac_key, patient_id))
