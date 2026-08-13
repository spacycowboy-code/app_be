#!/usr/bin/env python3
"""Targeted regression for the gaps found in the OASB / clawhub evaluation runs.

The cases are REFERENCED from the public labelled corpora at run time rather
than copied into this repo, so no attack payloads are stored here and the
expected verdicts stay tied to the upstream ground truth.

Covered:
  1. the two samples we missed
       MAL-INJECT-021     injection hidden inside an example-output block
       MAL-DATAEXFIL-015  bulk PII/financial/health export dressed as ETL
     plus their whole categories (prompt_injection, data_exfiltration), to show
     the rules change generalises instead of over-fitting two samples.
  2. the two real skills we wrongly hard-rejected (clawhub holdout), which must
     no longer come back as danger:
       stephancill/permissions-broker          credential brokering
       juan-xin-cai/openclaw-context-upgrade   agent config management
  3. a false-alarm guard: OASB benign + edge_case samples must not regress
     (benign -> pass, edge_case -> never danger).

Usage:  python3 skill_audit/run_regression.py [--endpoint URL]
"""
import argparse, json, os, random, re, subprocess, tempfile, time, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

OASB = "https://raw.githubusercontent.com/opena2a-org/oasb/main/corpus/"
CLAWHUB = ("https://huggingface.co/datasets/OpenClaw/clawhub-security-signals"
           "/resolve/main/data/eval_holdout.jsonl")
CLAWHUB_CACHE = "/tmp/eval_holdout.jsonl"
MISSED = {"MAL-INJECT-021", "MAL-DATAEXFIL-015"}
FP_SLUGS = {"stephancill/permissions-broker", "juan-xin-cai/openclaw-context-upgrade"}
N_BENIGN_GUARD = 60

ap = argparse.ArgumentParser()
ap.add_argument("--endpoint", default="http://localhost:9100/api/skills/evaluate")
# The API is behind a site-wide auth gate, so a run needs a bearer token; without
# one every case comes back HTTP401 and the run reports a regression that is not
# there.
ap.add_argument("--token", default=os.environ.get("AUDIT_TOKEN", ""),
                help="bearer token for the API (or AUDIT_TOKEN)")
# The benign guard dominates the cost: every case is one judge call. Keep the
# default that the recorded baseline used, but let a targeted run pay less.
ap.add_argument("--benign", type=int, default=None,
                help=f"benign guard sample size (default {N_BENIGN_GUARD})")
args = ap.parse_args()
if args.benign is not None:
    N_BENIGN_GUARD = args.benign
FM_RE = re.compile(r"^---\s+name:\s*(.+?)\s+description:\s*(.+?)\s+---\s*(.*)$", re.S)


def fetch_json(url):
    return json.load(urllib.request.urlopen(url, timeout=90))


def post(md, name):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(md)
        path = fh.name
    try:
        cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "180", "-X", "POST",
               args.endpoint, "-F", "eid=REGRESS", "-F", f"name={name}",
               "-F", f"file=@{path};type=text/markdown"]
        if args.token:
            cmd += ["-H", f"Authorization: Bearer {args.token}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout
        payload, _, code = out.rpartition("\n")
        return code.strip(), payload
    finally:
        os.unlink(path)


def evaluate(md, name):
    code, payload = post(md, name)
    if code == "400":                      # not a SKILL.md -> wrap minimally
        head = " ".join(md.split()[:25]) or "regression sample"
        code, payload = post(f"---\nname: {name}\ndescription: {head}\n---\n\n{md}", name)
    if code != "200":
        return f"HTTP{code}", []
    d = json.loads(payload)
    return d.get("state"), sorted({f.get("category") for f in d.get("findings", [])})


# --- collect cases ----------------------------------------------------------
cases = []          # (group, id, expectation, markdown, name)
for f, cat in (("batch-injection.json", "prompt_injection"),
               ("batch-dataexfil.json", "data_exfiltration")):
    for s in fetch_json(OASB + f):
        content = s["content"]
        md = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        group = "missed-sample" if s["id"] in MISSED else f"category:{cat}"
        cases.append((group, s["id"], "flag", md, s["id"]))
for f in ("batch-benign.json",):
    rows = fetch_json(OASB + f)
    ben = [s for s in rows if s.get("label") == "benign"]
    edge = [s for s in rows if s.get("label") == "edge_case"]
    random.seed(11)
    for s in random.sample(ben, min(N_BENIGN_GUARD, len(ben))):
        content = s["content"]
        md = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        cases.append(("benign", s["id"], "pass", md, s["id"]))
    for s in edge:
        content = s["content"]
        md = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        cases.append(("edge_case", s["id"], "not_danger", md, s["id"]))

if not os.path.exists(CLAWHUB_CACHE):
    print("downloading clawhub holdout ...", flush=True)
    req = urllib.request.Request(CLAWHUB, headers={"User-Agent": "regress"})
    with urllib.request.urlopen(req, timeout=600) as r, open(CLAWHUB_CACHE, "wb") as fh:
        fh.write(r.read())
for line in open(CLAWHUB_CACHE, encoding="utf-8"):
    row = json.loads(line)
    if row.get("skill_slug") in FP_SLUGS:
        md = row.get("skill_md_content") or ""
        m = FM_RE.match(md)
        if m:
            md = f"---\nname: {m.group(1).strip()}\ndescription: {m.group(2).strip()}\n---\n\n{m.group(3)}"
        cases.append(("was-false-positive", row["skill_slug"], "not_danger", md,
                      row["skill_slug"][:80]))

print(f"cases: {len(cases)}  ({dict(Counter(c[0] for c in cases))})", flush=True)


def run(case):
    group, cid, expect, md, name = case
    state, cats = evaluate(md, name)
    if expect == "flag":
        ok = state in ("danger", "suspect")
    elif expect == "pass":
        ok = state == "pass"
    else:                                   # not_danger
        ok = state in ("pass", "suspect")
    return {"group": group, "id": cid, "expect": expect, "state": state,
            "categories": cats, "ok": ok}


t0 = time.time()
results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, r in enumerate(ex.map(run, cases), 1):
        results.append(r)
        if i % 25 == 0:
            print(f"{i}/{len(cases)} ({time.time()-t0:.0f}s)", flush=True)

print(f"\n=== regression summary ({time.time()-t0:.0f}s) ===")
failures = []
for g in ("missed-sample", "category:prompt_injection", "category:data_exfiltration",
          "was-false-positive", "benign", "edge_case"):
    sub = [r for r in results if r["group"] == g]
    if not sub:
        continue
    good = sum(1 for r in sub if r["ok"])
    print(f"  {g:28} {good}/{len(sub)} ok    states={dict(Counter(r['state'] for r in sub))}")
    failures += [r for r in sub if not r["ok"]]
if failures:
    print("\nFAILURES:")
    for r in failures:
        print(f"  {r['group']:24} {r['id']:34} expect={r['expect']:10} got={r['state']} {r['categories']}")
else:
    print("\nall regression expectations met")
with open("/tmp/regression_results.json", "w") as fh:
    json.dump(results, fh, ensure_ascii=False, indent=1)
