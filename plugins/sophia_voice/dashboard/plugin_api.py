from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter()


def _base_url() -> str:
    return os.getenv("SOPHIA_VOICE_URL", "http://127.0.0.1:8765").rstrip("/")


def _proxy(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        resp = requests.request(method, f"{_base_url()}{path}", timeout=kwargs.pop("timeout", 20), **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Sophia voice sidecar unavailable: {exc}") from exc


class TranscriptBody(BaseModel):
    transcript: str
    session_id: str = "dashboard"
    user_id: str = "default"


class Neo4jEnrollBody(BaseModel):
    neo4j_uri: Optional[str] = None
    neo4j_user: Optional[str] = None
    neo4j_pass: Optional[str] = None
    neo4j_database: Optional[str] = None
    user_id: str
    speaker_node_id: Optional[str] = None
    speaker_name: Optional[str] = None
    limit: int = 200


@router.get("/status")
def status() -> Dict[str, Any]:
    return _proxy("GET", "/status", timeout=3)


@router.get("/memory-graph/status")
def memory_graph_status() -> Dict[str, Any]:
    return _proxy("GET", "/memory-graph/status", timeout=3)


@router.get("/events")
def events(after_id: int = 0, session_id: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"after_id": after_id}
    if session_id:
        params["session_id"] = session_id
    return _proxy("GET", "/events", params=params, timeout=5)


@router.post("/intent")
def intent(body: TranscriptBody) -> Dict[str, Any]:
    return _proxy("POST", "/intent", json={"transcript": body.transcript}, timeout=10)


@router.post("/chat")
def chat(body: TranscriptBody) -> Dict[str, Any]:
    return _proxy(
        "POST",
        "/voice-chat",
        json={"transcript": body.transcript, "session_id": body.session_id, "user_id": body.user_id},
        timeout=60,
    )


@router.post("/voiceprints/train-neo4j")
def train_neo4j(body: Neo4jEnrollBody) -> Dict[str, Any]:
    return _proxy("POST", "/voiceprints/train-neo4j", json=body.model_dump(exclude_none=True), timeout=120)
