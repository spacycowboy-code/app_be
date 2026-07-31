"""Skill-audit judge service (host-side).

A thin HTTP wrapper around the kiro-cli semantic judge. It runs where kiro-cli
is installed and logged in (the host), holds the only path to the model, and
can be egress-restricted independently. The (credential-less) backend container
calls it via AUDIT_JUDGE_URL.

Run (on the host, in the venv with kiro-cli on PATH):
    uvicorn app.skill_audit_judge_service:app --host 127.0.0.1 --port 8790
"""
from dataclasses import asdict

from fastapi import FastAPI, Header, HTTPException

from app.config import get_settings
from app.services.skill_audit.judge import run_judge

app = FastAPI(title="skill-audit judge service")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/judge")
def judge(body: dict, x_judge_token: str | None = Header(default=None)) -> dict:
    s = get_settings()
    if s.audit_judge_token and x_judge_token != s.audit_judge_token:
        raise HTTPException(status_code=401, detail="invalid judge token")
    result = run_judge(
        rules=body.get("rules", ""),
        skill_text=body.get("skill_text", ""),
        det_summary=body.get("det_summary", ""),
        kiro_bin=s.audit_kiro_bin,
        # The caller picks the model (primary or the confirmation pass).
        model=(body.get("model") or s.audit_kiro_model),
        work_dir=s.audit_work_dir,
        timeout=s.audit_judge_timeout,
    )
    return asdict(result)
