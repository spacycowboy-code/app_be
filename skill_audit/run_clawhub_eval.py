#!/usr/bin/env python3
"""In-domain evaluation: run our audit over real marketplace skills from
OpenClaw/clawhub-security-signals (eval_holdout split).

Phase 1 measures what our own corpus cannot: the false-positive / review rate on
REAL skills (2119 clean ones in the holdout), plus how we score the labelled
suspicious/malicious ones. Only skill_md_content is sent (text layer); bundles
are handled by the static layer and are evaluated separately.

Their labels come from scanners (clawscan is itself an LLM judge), so this is an
agreement measurement, not ground truth - except on the clean subset, where a
flag from us is a review burden regardless of who labelled it.
"""
import json, os, random, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

HOLDOUT_URL = ("https://huggingface.co/datasets/OpenClaw/clawhub-security-signals"
               "/resolve/main/data/eval_holdout.jsonl")
LOCAL = "/tmp/eval_holdout.jsonl"
OUT = "/tmp/clawhub_eval.jsonl"
ENDPOINT = "http://localhost:9100/api/skills/evaluate"
N_CLEAN, N_SUSPICIOUS, WORKERS = 250, 150, 4

if not os.path.exists(LOCAL):
    print("downloading eval_holdout.jsonl ...", flush=True)
    req = urllib.request.Request(HOLDOUT_URL, headers={"User-Agent": "eval"})
    with urllib.request.urlopen(req, timeout=600) as r, open(LOCAL, "wb") as f:
        f.write(r.read())
rows = [json.loads(l) for l in open(LOCAL, encoding="utf-8") if l.strip()]
print(f"holdout rows: {len(rows)}", flush=True)

by = {"clean": [], "suspicious": [], "malicious": []}
for r in rows:
    v = r.get("clawscan_verdict")
    if v in by:
        by[v].append(r)
random.seed(20260728)
sample = (by["malicious"]
          + random.sample(by["suspicious"], min(N_SUSPICIOUS, len(by["suspicious"])))
          + random.sample(by["clean"], min(N_CLEAN, len(by["clean"]))))
random.shuffle(sample)
print(f"sample: {len(sample)} "
      f"(malicious={len(by['malicious'])}, suspicious={min(N_SUSPICIOUS,len(by['suspicious']))}, "
      f"clean={min(N_CLEAN,len(by['clean']))})", flush=True)

done = 0
t0 = time.time()
lock_out = open(OUT, "w")


FM_RE = re.compile(r"^---\s+name:\s*(.+?)\s+description:\s*(.+?)\s+---\s*(.*)$", re.S)


def rebuild_md(row):
    """The dataset stores skill_md_content with all newlines collapsed to spaces,
    so the YAML frontmatter arrives on one line and our validator rejects it.
    Rebuild a valid SKILL.md: real frontmatter when it can be parsed back,
    otherwise synthesise one from the slug. Returns (markdown, provenance).
    """
    md = row.get("skill_md_content") or ""
    m = FM_RE.match(md)
    if m:
        name, desc, body = m.group(1).strip(), m.group(2).strip(), m.group(3)
        return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}", "parsed"
    slug = (row.get("skill_slug") or "skill").split("/")[-1]
    head = " ".join(md.split()[:25]) or "no description available"
    return f"---\nname: {slug}\ndescription: {head}\n---\n\n{md}", "synthetic"


def run_one(row):
    """POST one skill's SKILL.md to evaluate; return our verdict + their labels."""
    import subprocess, tempfile
    md, provenance = rebuild_md(row)
    rec = {
        "slug": row.get("skill_slug"),
        "their_clawscan": row.get("clawscan_verdict"),
        "their_static": row.get("static_status"),
        "their_skillspector_sev": row.get("skillspector_severity"),
        "has_bundle": bool(row.get("skill_bundle_content")),
        "md_len": len(md),
        "frontmatter": provenance,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as f:
        f.write(md)
        path = f.name
    try:
        out = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "180",
             "-X", "POST", ENDPOINT,
             "-F", "eid=EVAL",
             "-F", "name=" + (row.get("skill_slug") or "eval")[:80],
             "-F", f"file=@{path};type=text/markdown"],
            capture_output=True, text=True, timeout=240).stdout
        payload, _, code = out.rpartition("\n")
        if code.strip() == "200":
            data = json.loads(payload)
            rec["our_state"] = data.get("state")
            rec["our_categories"] = sorted({f.get("category") for f in data.get("findings", [])})
        else:
            rec["our_state"] = f"HTTP{code.strip()}"
            rec["our_categories"] = []
            rec["err"] = payload[:200]
    except Exception as e:
        rec["our_state"] = f"ERR:{type(e).__name__}"
        rec["our_categories"] = []
    finally:
        os.unlink(path)
    return rec


with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for rec in ex.map(run_one, sample):
        done += 1
        lock_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lock_out.flush()
        if done % 20 == 0:
            print(f"{done}/{len(sample)}  ({time.time()-t0:.0f}s)", flush=True)
lock_out.close()
print(f"=== done {done} in {time.time()-t0:.0f}s -> {OUT}", flush=True)
