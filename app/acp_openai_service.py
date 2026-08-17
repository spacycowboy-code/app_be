"""Chat Completions in front of the local kiro agent.

The skill scanner is the audit engine: it runs the deterministic analyzers, then its own
semantic and meta analyzers, and returns one report. Those two analyzers need an
OpenAI-compatible endpoint; this is that endpoint, backed by ``kiro-cli acp``.

Point the scanner at it with:
    SKILL_SCANNER_LLM_PROVIDER=openai
    SKILL_SCANNER_LLM_BASE_URL=http://<this host>:<port>/v1
    SKILL_SCANNER_LLM_API_KEY=<any non-empty value>
    SKILL_SCANNER_LLM_MODEL=openai/<model>     # the openai/ prefix is required

Run where kiro-cli is installed and logged in:
    uvicorn app.acp_openai_service:app --host 0.0.0.0 --port 8088
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.services.skill_audit.acp import (
    AcpError,
    complete_json,
    render_prompt,
    validate_model,
)

log = logging.getLogger("acp-openai")
app = FastAPI(title="acp -> chat completions adapter")


@app.on_event("startup")
def check_model() -> None:
    """Refuse to serve with a model name the agent would silently ignore.

    ``kiro-cli acp`` accepts an unknown --model and serves the default instead, so a typo
    would make every audit record's model attribution false. Fail here instead.
    """
    s = get_settings()
    try:
        validate_model(s.audit_kiro_bin, s.audit_kiro_model)
    except AcpError as exc:
        raise RuntimeError(str(exc)) from exc
    except (OSError, Exception) as exc:  # noqa: BLE001 - startup must say why it failed
        log.warning("could not verify model %r: %s", s.audit_kiro_model, exc)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
def models() -> dict:
    s = get_settings()
    return {"object": "list",
            "data": [{"id": s.audit_kiro_model or "kiro", "object": "model", "owned_by": "kiro"}]}


# Deliberately sync: the ACP exchange is blocking subprocess I/O, and a sync handler runs
# in uvicorn's worker threadpool instead of stalling the event loop.
@app.post("/v1/chat/completions")
def chat_completions(body: dict) -> dict:
    s = get_settings()
    prompt = render_prompt(body.get("messages") or [])
    if not prompt:
        raise HTTPException(status_code=400, detail="no message content")
    started = time.time()
    try:
        text = complete_json(
            bin_path=s.audit_kiro_bin,
            model=s.audit_kiro_model,
            agent=s.audit_acp_agent,
            cwd=s.audit_work_dir,
            timeout=s.audit_judge_timeout,
            prompt=prompt,
        )
    except AcpError as exc:
        # The scanner turns this into an analyzer-failed finding, which the gate reads as
        # "this layer did not run". That is the honest outcome; do not fake a verdict.
        log.warning("acp failed after %.1fs: %s", time.time() - started, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log.info("completion: %d chars in -> %d chars out in %.1fs",
             len(prompt), len(text), time.time() - started)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(started),
        "model": body.get("model") or s.audit_kiro_model or "kiro",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        # Token counts are not observable through ACP. Report zeros rather than invent
        # numbers a caller might budget against.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
