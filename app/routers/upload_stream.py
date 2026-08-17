"""Streaming variant of evaluate, so the wait can be narrated truthfully.

An audit takes tens of seconds (measured over 44 real skills: p50 42s, p90 78s, max 91s).
A fixed animation finishes long before the answer does, which leaves the person watching
a completed progress bar and nothing happening. The scanner narrates its own phase
boundaries on stderr, so those can be forwarded instead of invented.

The plain JSON endpoint stays: publish re-runs the audit as the authoritative gate and has
no use for progress, and a client that cannot read a stream is not left without an option.
"""
from __future__ import annotations

import asyncio
import json
import queue
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.skill_audit import gate
from app.services.skill_audit.pipeline import audit_skill
from app.services.skill_audit.types import AuditResult

# Order matters: it is the order the scanner reaches them in, which is what the UI shows.
STAGES: tuple[tuple[str, str], ...] = (
    ("deterministic", "Static rules over every bundled file"),
    ("semantic", "Semantic review of the instructions and configuration"),
    ("arbitration", "Second pass to drop false positives"),
    ("reporting", "Building the report"),
)


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def register(router: APIRouter, read_upload, checks_out, findings_out, normalize_eid) -> None:
    """Attach the streaming endpoint, reusing the JSON endpoint's helpers."""

    @router.post("/evaluate/stream")
    async def evaluate_skill_stream(                     # noqa: D401 - route
        name: str = Form(...),
        eid: str = Form(""),
        instructions: str = Form(""),
        file: UploadFile | None = File(None),
        db: Session = Depends(get_db),
    ) -> StreamingResponse:
        eid_norm = normalize_eid(eid)
        definition, artifact_bytes, artifact_filename = await read_upload(file, instructions)
        events: queue.Queue = queue.Queue()

        def run() -> AuditResult:
            return audit_skill(
                name=name, skill_md=definition, artifact_bytes=artifact_bytes,
                artifact_filename=artifact_filename, uploader_eid=eid_norm or None,
                on_progress=events.put,
            )

        async def stream():
            loop = asyncio.get_running_loop()
            task = loop.run_in_executor(None, run)
            # The first phase is under way as soon as the scan starts; the scanner only
            # announces a phase when it *finishes* one, so "deterministic" is the state we
            # are in until it says otherwise.
            yield _sse("stage", {"stage": "deterministic", "stages": [
                {"id": s, "label": label} for s, label in STAGES]})
            while not task.done():
                try:
                    stage = await loop.run_in_executor(None, events.get, True, 1.0)
                except queue.Empty:
                    yield _sse("ping", {})               # keep proxies from idling us out
                    continue
                yield _sse("stage", {"stage": stage})
            audit: AuditResult = await task
            reference = uuid.uuid4().hex
            if audit.is_danger:
                gate.record(db, reference, audit, skill_id=None, skill_name=name,
                            uploader_eid=eid_norm or None)
                db.commit()
            yield _sse("result", {
                "state": audit.state, "reference": reference, "reason": audit.reason,
                "checks": [c.model_dump(by_alias=True) for c in checks_out(audit)],
                "findings": [f.model_dump(by_alias=True) for f in findings_out(audit)],
            })

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            # Without this a buffering proxy holds the events until the response ends,
            # which turns a live narration back into a blank wait.
            "X-Accel-Buffering": "no",
        })
