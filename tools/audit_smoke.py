#!/usr/bin/env python
"""Post-deploy smoke check for the skill audit, run against a live environment.

Why this exists as a committed script rather than something typed out each time: these are
the checks that only a deployment can answer, and they are the same ones every time. Written
by hand they drift, and the numbers they produce cannot be compared between runs.

What the unit suite cannot cover, and this can:

* the scanner, the model adapter and the agent definition are actually reachable *in this
  image*, with this policy — three of the bugs in the refactor were only visible here
* the recorded actor comes from the session token on the real HTTP path
* the streamed progress carries the scanner's own phase boundaries end to end
* a second submission of the same bytes reuses the verdict instead of paying again

Usage (inside the backend container, which holds the token signing key and the DB URL)::

    python -m tools.audit_smoke                 # full check
    python -m tools.audit_smoke --quick         # skip the streamed run (saves ~30s)
    python -m tools.audit_smoke --base http://localhost:8000

Exit status is 0 only when every check passed, so it can gate a deploy.

It creates one skill and deletes it again, including its audit records. Nothing else in the
environment is touched, and it never pushes to the skill repo (the upload it makes is
published only if the audit says pass, exactly as any upload would be).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/app")

SKILL_MD = (
    "---\n"
    "name: audit-smoke\n"
    "description: A benign skill used by the post-deploy smoke check. Safe to delete.\n"
    "---\n\n"
    "# Audit Smoke\n\n"
    "Read the CSV files in the working directory and summarise their columns.\n\n"
    "## Usage\n\n"
    "Run `python scripts/report.py --input data.csv` and read the output.\n"
)

# The session holds one EID; the request body claims another. The recorded actor has to be
# the first one — see acting_eid.
SIGNED_IN = "ESMOKE1"
CLAIMED = "ESMOKE2"


class Checks:
    """Collects results so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((ok, name, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for ok, _n, _d in self.rows if not ok)


def _multipart(fields: dict[str, str], body: str) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
        for k, v in fields.items()
    ]
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"SKILL.md\"\r\n"
        f"Content-Type: text/markdown\r\n\r\n{body}\r\n"
    )
    return boundary, ("".join(parts) + f"--{boundary}--\r\n").encode()


def _post(base: str, path: str, fields: dict[str, str], token: str | None,
          timeout: int = 300) -> tuple[int, dict, float]:
    boundary, payload = _multipart(fields, SKILL_MD)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=payload, method="POST", headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}"), time.monotonic() - started
        except ValueError:
            return exc.code, {"detail": raw[:200].decode("utf-8", "replace")}, time.monotonic() - started


def _stream(base: str, path: str, fields: dict[str, str], token: str,
            timeout: int = 300) -> tuple[int, dict[str, str], list[tuple[float, str, dict]]]:
    """Read an SSE response, returning (status, headers, [(offset, event, data)])."""
    boundary, payload = _multipart(fields, SKILL_MD)
    req = urllib.request.Request(base + path, data=payload, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}",
    })
    events: list[tuple[float, str, dict]] = []
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        buffer = b""
        while True:
            chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                name = data = ""
                for line in frame.decode("utf-8", "replace").splitlines():
                    if line.startswith("event: "):
                        name = line[7:].strip()
                    elif line.startswith("data: "):
                        data += line[6:]
                if not name or name == "ping":
                    continue
                try:
                    parsed = json.loads(data) if data else {}
                except ValueError:
                    parsed = {}
                events.append((time.monotonic() - started, name, parsed))
        return resp.status, headers, events


def _cleanup(name: str) -> str:
    """Remove the skill and audit records this check created."""
    from app.database import SessionLocal
    from app.models.audit import RpAiSkillAuditRecord
    from app.models.skill import RpAiSkill

    removed = []
    with SessionLocal() as db:
        for row in db.query(RpAiSkill).filter(RpAiSkill.name == name).all():
            db.delete(row)
            removed.append("skill")
        for rec in db.query(RpAiSkillAuditRecord).filter(
                RpAiSkillAuditRecord.skill_name == name).all():
            db.delete(rec)
            removed.append("record")
        db.commit()
    return ", ".join(removed) or "nothing to remove"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--quick", action="store_true", help="skip the streamed run")
    args = ap.parse_args()

    from app.services.auth_tokens import create_token

    checks = Checks()
    token, _ttl = create_token(SIGNED_IN, display_name="Audit Smoke")
    name = "audit-smoke-" + uuid.uuid4().hex[:8]

    print(f"  target {args.base}, skill {name}")

    # 1. The audit runs at all, on the real path, with every layer.
    code, body, seconds = _post(args.base, "/api/skills/evaluate",
                                {"eid": CLAIMED, "name": name}, token)
    state = body.get("state")
    checks.add(code == 200 and state in ("pass", "suspect", "danger"),
               "evaluate answers", f"HTTP {code} state={state} in {seconds:.1f}s")
    checks.add(state == "pass",
               "a benign skill is not blocked",
               f"state={state} reason={body.get('reason') or '(none)'}")

    # 2. Authentication is required. Without it there is no verified actor to record.
    code_anon, _b, _s = _post(args.base, "/api/skills/evaluate",
                              {"eid": CLAIMED, "name": name}, None, timeout=60)
    checks.add(code_anon == 401, "an unauthenticated evaluate is refused", f"HTTP {code_anon}")

    # 3. Publish, then check what was recorded and whether the verdict was reused.
    code_up, body_up, seconds_up = _post(args.base, "/api/skills/upload",
                                        {"eid": CLAIMED, "name": name, "categoryId": "public"},
                                        token)
    checks.add(code_up == 201, "upload succeeds", f"HTTP {code_up} in {seconds_up:.1f}s")
    checks.add(seconds_up < 5.0, "the verdict is reused, not paid for twice",
               f"{seconds_up:.1f}s (a fresh audit takes tens of seconds)")

    from app.database import SessionLocal
    from app.models.audit import RpAiSkillAuditRecord
    from app.models.skill import RpAiSkill

    with SessionLocal() as db:
        row = db.query(RpAiSkill).filter(RpAiSkill.name == name).one_or_none()
        checks.add(row is not None, "the skill was stored")
        if row is not None:
            checks.add(row.uploader_eid == SIGNED_IN,
                       "the recorded uploader is the signed-in caller",
                       f"uploader_eid={row.uploader_eid}, body claimed {CLAIMED}")
        rec = (db.query(RpAiSkillAuditRecord)
                 .filter(RpAiSkillAuditRecord.skill_name == name)
                 .order_by(RpAiSkillAuditRecord.id.desc()).first())
        checks.add(rec is not None, "an audit record was written")
        if rec is not None:
            used = set((rec.analyzers_used or "").replace(" ", "").split(","))
            checks.add("llm_analyzer" in used, "the semantic layer ran", f"analyzers={used}")
            checks.add("meta_analyzer" in used, "arbitration ran", f"analyzers={used}")
            checks.add(bool(rec.scanner_version), "the scanner version was recorded",
                       f"{rec.scanner_version}")
            checks.add(bool(rec.policy_fingerprint), "the policy fingerprint was recorded",
                       f"{rec.policy_fingerprint}")
            checks.add(not rec.incomplete, "no layer reported itself incomplete",
                       f"incomplete={rec.incomplete or '(none)'}")

    # 4. The streamed variant carries the scanner's phases, and always concludes.
    if not args.quick:
        stream_name = name + "-stream"
        status, headers, events = _stream(args.base, "/api/skills/evaluate/stream",
                                         {"eid": CLAIMED, "name": stream_name}, token)
        kinds = [kind for _o, kind, _d in events]
        stages = [(offset, data.get("stage")) for offset, kind, data in events if kind == "stage"]
        checks.add(status == 200 and headers.get("content-type", "").startswith("text/event-stream"),
                   "the stream is an event stream", f"HTTP {status} {headers.get('content-type')}")
        checks.add(headers.get("x-accel-buffering") == "no",
                   "buffering is disabled for this response",
                   "without it a proxy holds every event until the end")
        checks.add(bool(kinds) and kinds[-1] in ("result", "error"),
                   "the stream concludes", f"last event = {kinds[-1] if kinds else '(none)'}")
        checks.add(len(stages) >= 3, "the scanner's phases came through",
                   "  ".join(f"+{o:.1f}s {s}" for o, s in stages))
        rising = all(b[0] > a[0] for a, b in zip(stages, stages[1:]))
        checks.add(rising and len({s for _o, s in stages}) == len(stages),
                   "the phases are distinct and in order",
                   "a fixed animation would be evenly spaced; these are not")
        print(f"  cleanup ({stream_name}): {_cleanup(stream_name)}")

    print(f"  cleanup ({name}): {_cleanup(name)}")
    print(f"  {len(checks.rows) - checks.failed}/{len(checks.rows)} checks passed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
