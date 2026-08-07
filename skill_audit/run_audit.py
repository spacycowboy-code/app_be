#!/usr/bin/env python3
"""Track-A skill-audit semantic-judge eval harness.

Per skill: lightweight secret-scan + redact -> build prompt (rules + skill +
deterministic findings) -> kiro-cli headless judge (model=auto, tool-less) ->
parse JSON -> framework injection self-checks (schema / evidence-grounding /
consistency). Then tally vs expected labels.

The secret layer here is a small regex stand-in; production uses detect-secrets
(validated separately). This harness exercises the semantic judge on the
eval_fixtures/ and, with --all, the real skill repo.

Usage:
    python3 run_audit.py                 # eval_fixtures only
    SKILL_REPO=/path/to/rp-ai-skill python3 run_audit.py --all   # + real skills
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = (HERE / "rules.md").read_text(encoding="utf-8")
FIXTURES = HERE / "eval_fixtures"
REAL = Path(os.environ.get("SKILL_REPO", "")) if os.environ.get("SKILL_REPO") else None
CWD = str(HERE.parent)  # be repo root (so `.kiro/agents` is discoverable if --agent is used)

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
SECRET_PATS = [
    ("tavily", re.compile(r"tvly-(?!YOUR)[A-Za-z0-9]{16,}")),
    ("openai", re.compile(r"sk-(proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws_akid", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("privkey", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def secret_scan_and_redact(text):
    findings, redacted = [], text
    for name, pat in SECRET_PATS:
        if pat.search(text):
            findings.append(name)
        redacted = pat.sub("<REDACTED_SECRET>", redacted)
    return findings, redacted


def clean(out):
    out = ANSI.sub("", out)
    lines = []
    for ln in out.splitlines():
        s = ln.rstrip()
        if s.strip() == ">":
            continue
        if s.startswith("> "):
            s = s[2:]
        lines.append(s)
    return "\n".join(lines).strip()


def extract_json(s):
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def judge(skill_text):
    det, redacted = secret_scan_and_redact(skill_text)
    prompt = (
        RULES
        + "\n\n=== SKILL UNDER AUDIT ===\n" + redacted
        + "\n\n=== DETERMINISTIC FINDINGS (secret scan) ===\n"
        + (", ".join(det) if det else "(none)")
        + "\n\nNow output ONLY the JSON verdict."
    )
    t = time.time()
    try:
        p = subprocess.run(
            ["kiro-cli", "chat", "--no-interactive", "--trust-tools=", prompt],
            capture_output=True, text=True, timeout=200, cwd=CWD,
        )
        return det, extract_json(clean(p.stdout)), round(time.time() - t, 1)
    except subprocess.TimeoutExpired:
        return det, None, round(time.time() - t, 1)


def collect():
    items = []
    for d in sorted(FIXTURES.glob("*")):
        md = d / "SKILL.md"
        if md.is_file():
            items.append((f"fixture/{d.name}", md, "good" if d.name == "benign-tricky" else "bad"))
    if "--all" in sys.argv and REAL and REAL.is_dir():
        for md in sorted(REAL.rglob("SKILL.md")):
            items.append((f"real/{md.parent.name}", md, "good"))
    elif "--all" in sys.argv:
        print("  [--all ignored: set SKILL_REPO=/path/to/rp-ai-skill]")
    return items


def main():
    items = collect()
    print(f"skills: {len(items)}\n")
    rows = []
    for sid, md, label in items:
        text = md.read_text(encoding="utf-8")
        det, v, secs = judge(text)
        if v is None:
            print(f" [PARSE-FAIL] {sid[:44]:44} {secs}s")
            rows.append((sid, label, None, False, True))
            continue
        verdict, fs = v.get("verdict"), (v.get("findings") or [])
        schema_ok = verdict in ("block", "flag", "pass") and isinstance(fs, list)
        grounded = all(f.get("evidence_span", "") in text for f in fs) if fs else True
        cats = ",".join(sorted({f.get("category", "?") for f in fs}))
        print(f" {sid[:44]:44} {str(verdict):5} n={len(fs)} [{cats[:34]}] "
              f"schema={'Y' if schema_ok else 'N'} grounded={'Y' if grounded else 'N'} {secs}s")
        rows.append((sid, label, verdict, schema_ok, grounded))

    bad = [r for r in rows if r[1] == "bad"]
    good = [r for r in rows if r[1] == "good"]
    print("\n=== TALLY ===")
    print(f"bad(should block):  {sum(1 for r in bad if r[2] == 'block')}/{len(bad)} blocked")
    print(f"good(should pass):  {sum(1 for r in good if r[2] == 'pass')}/{len(good)} pass, "
          f"FP={sum(1 for r in good if r[2] in ('block', 'flag'))}")
    print(f"parse-fail: {sum(1 for r in rows if r[2] is None)} | "
          f"schema-bad: {sum(1 for r in rows if r[2] and not r[3])} | "
          f"ungrounded: {sum(1 for r in rows if r[2] and not r[4])}")


if __name__ == "__main__":
    main()
