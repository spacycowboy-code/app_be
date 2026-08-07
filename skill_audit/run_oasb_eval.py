#!/usr/bin/env python3
"""Run our audit gate against the OASB corpus (Apache-2.0 ground-truth benchmark
for agent-skill scanners).

The expected verdict per OASB category is DECLARED HERE, before the run, so the
result is a conformance measurement against our own red lines rather than a
post-hoc rationalisation:

    danger  (hard reject) : credential_exfiltration, prompt_injection,
                            data_exfiltration, heartbeat_rce
    suspect (review)      : persistence, privilege_escalation
    out of scope          : social_engineering  (behaviour, not dangerous content)
    benign                : must pass
    edge_case             : must NOT be hard-rejected (pass or suspect is fine)
"""
import json, os, random, subprocess, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

RAW = "https://raw.githubusercontent.com/opena2a-org/oasb/main/corpus/"
FILES = ["batch-benign.json", "batch-dataexfil.json", "batch-exfil.json",
         "batch-injection.json", "batch-persist.json", "batch-priv.json",
         "batch-rce.json", "batch-social.json"]
ENDPOINT = "http://localhost:9100/api/skills/evaluate"
OUT = "/tmp/oasb_eval.jsonl"
WORKERS = 4

EXPECTED = {
    "credential_exfiltration": "danger",
    "prompt_injection": "danger",
    "data_exfiltration": "danger",
    "heartbeat_rce": "danger",
    "persistence": "suspect",
    "privilege_escalation": "suspect",
    "social_engineering": "out_of_scope",
}

samples = []
for f in FILES:
    try:
        d = json.load(urllib.request.urlopen(RAW + f, timeout=90))
    except Exception as e:
        print(f"fetch {f}: ERROR {e}", flush=True)
        continue
    if isinstance(d, dict):
        d = d.get("samples") or d.get("cases") or []
    samples += d
print(f"loaded {len(samples)} labeled samples", flush=True)


def expected_for(s):
    lab = s.get("label")
    if lab == "benign":
        return "pass"
    if lab == "edge_case":
        return "not_danger"
    return EXPECTED.get(s.get("category"), "unmapped")


def post(md, name):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(md)
        path = fh.name
    try:
        out = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "180",
             "-X", "POST", ENDPOINT, "-F", "eid=EVAL", "-F", f"name={name}",
             "-F", f"file=@{path};type=text/markdown"],
            capture_output=True, text=True, timeout=240).stdout
        payload, _, code = out.rpartition("\n")
        return code.strip(), payload
    finally:
        os.unlink(path)


def run_one(s):
    content = s.get("content")
    md = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    name = (s.get("id") or "oasb")[:80]
    rec = {"id": s.get("id"), "label": s.get("label"), "category": s.get("category"),
           "artifact_type": s.get("artifactType"), "source": s.get("source"),
           "expected": expected_for(s), "frontmatter": "original"}
    code, payload = post(md, name)
    if code == "400":
        # Not a SKILL.md (mcp_tool / system_prompt / agent_config have no
        # frontmatter). Wrap it so the same content reaches the audit.
        head = " ".join(md.split()[:25]) or "benchmark sample"
        wrapped = f"---\nname: {name}\ndescription: {head}\n---\n\n{md}"
        rec["frontmatter"] = "wrapped"
        code, payload = post(wrapped, name)
    if code == "200":
        data = json.loads(payload)
        rec["our_state"] = data.get("state")
        rec["our_categories"] = sorted({f.get("category") for f in data.get("findings", [])})
    else:
        rec["our_state"] = f"HTTP{code}"
        rec["our_categories"] = []
        rec["err"] = payload[:150]
    return rec


t0 = time.time()
random.seed(7)
random.shuffle(samples)
with open(OUT, "w") as fh, ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for i, rec in enumerate(ex.map(run_one, samples), 1):
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        if i % 25 == 0:
            print(f"{i}/{len(samples)} ({time.time()-t0:.0f}s)", flush=True)
print(f"=== done {len(samples)} in {time.time()-t0:.0f}s -> {OUT}", flush=True)
