#!/usr/bin/env python3
"""Measure our gate on the OASB v2 corpus using OASB's own verdict rule, so the
numbers can be read next to their published leaderboard.

Scope: only `artifactType == "skill"` samples (290: 205 malicious, 68 benign,
17 edge). Our gate audits skill uploads; the rest of the corpus is mcp_tool /
agent_config / system_prompt / soul, which this product does not accept, so
running them here would measure something we do not claim.

OASB verdict rule (BENCHMARK-RESULTS.md section 3, and the `note` field of
benchmark-results-v6.json): a sample counts as flagged malicious when the
scanner produces at least one high/critical ATTACK finding. Posture / hardening
findings are excluded. We emit no posture findings, so the rule reduces to:
at least one finding at severity high or critical.

Metrics are computed over malicious + benign; edge cases are reported apart,
the same way OASB does.

    python3 skill_audit/run_oasb_v2_eval.py [--limit N]
"""
import argparse, collections, json, os, subprocess, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

CORPUS = "https://raw.githubusercontent.com/opena2a-org/oasb/main/corpus/v2.json"
CACHE = "/tmp/oasb_v2.json"
ENDPOINT = os.environ.get("EVAL_ENDPOINT", "http://localhost:9100/api/skills/evaluate")
OUT = os.environ.get("EVAL_OUT", "/tmp/oasb_v2_eval.jsonl")
WORKERS = 4
FLAG_SEVERITIES = {"high", "critical"}

# Published OASB numbers, for the summary print only (full 4,245-sample corpus,
# all artifact types; see BENCHMARK-RESULTS.md).
PUBLISHED = {
    "HMA Full Pipeline": dict(f1=.829, precision=.832, recall=.826, fpr=.0116),
    "HMA Static (regex)": dict(f1=.675, precision=.993, recall=.511, fpr=.0003),
}


def load_samples():
    if not os.path.exists(CACHE):
        req = urllib.request.Request(CORPUS, headers={"User-Agent": "oasb-eval"})
        with urllib.request.urlopen(req, timeout=600) as r:
            open(CACHE, "wb").write(r.read())
    d = json.load(open(CACHE))
    rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
    return [r for r in rows if r.get("artifactType") == "skill"]


def post(md, name):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
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
    md = s.get("content") if isinstance(s.get("content"), str) else json.dumps(s.get("content"), ensure_ascii=False)
    name = (s.get("id") or "oasb")[:80]
    rec = {"id": s.get("id"), "label": s.get("label"), "category": s.get("category"),
           "source": s.get("source"), "frontmatter": "original"}
    code, payload = post(md, name)
    if code == "400":  # no frontmatter: wrap so the same content reaches the audit
        head = " ".join(md.split()[:25]) or "benchmark sample"
        code, payload = post(f"---\nname: {name}\ndescription: {head}\n---\n\n{md}", name)
        rec["frontmatter"] = "wrapped"
    if code == "200":
        data = json.loads(payload)
        findings = data.get("findings", [])
        rec["our_state"] = data.get("state")
        rec["severities"] = sorted({f.get("severity") for f in findings})
        rec["our_categories"] = sorted({f.get("category") for f in findings})
        # OASB rule: flagged iff at least one high/critical finding
        rec["flagged"] = any(f.get("severity") in FLAG_SEVERITIES for f in findings)
    else:
        rec["our_state"] = f"HTTP{code}"
        rec["severities"] = rec["our_categories"] = []
        rec["flagged"] = None          # fail-closed in production; excluded here
        rec["err"] = payload[:150]
    return rec


def summarise(recs):
    # The published results were produced on the categorised set. The corpus file
    # has since grown registry stubs labelled malicious whose content is only a
    # name ("Agent skill: podcast-tour"), with no instruction to detect; they are
    # counted separately so the headline stays on the same footing as the
    # published numbers.
    mal = [r for r in recs if r["label"] == "malicious" and r["category"]]
    stubs = [r for r in recs if r["label"] == "malicious" and not r["category"]]
    ben = [r for r in recs if r["label"] == "benign"]
    edge = [r for r in recs if r["label"] == "edge_case"]
    err = [r for r in recs if r["flagged"] is None]

    tp = sum(1 for r in mal if r["flagged"])
    fn = sum(1 for r in mal if r["flagged"] is False)
    fp = sum(1 for r in ben if r["flagged"])
    tn = sum(1 for r in ben if r["flagged"] is False)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec_ = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec_ / (prec + rec_) if prec + rec_ else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0

    print(f"\nsamples: {len(recs)}  (categorised malicious {len(mal)}, benign {len(ben)}, "
          f"edge {len(edge)}, uncategorised registry stubs {len(stubs)})")
    if err:
        print(f"errors excluded from metrics: {len(err)}")
    print("\n=== OASB verdict rule: flagged = at least one high/critical finding ===")
    print(f"  TP {tp}  FN {fn}  FP {fp}  TN {tn}")
    print(f"  precision {prec:.3f}   recall {rec_:.3f}   F1 {f1:.3f}   FPR {fpr:.4f}")
    print("\n  published, full 4,245-sample corpus, all artifact types:")
    for k, v in PUBLISHED.items():
        print(f"    {k:<20} F1 {v['f1']:.3f}  P {v['precision']:.3f}  "
              f"R {v['recall']:.3f}  FPR {v['fpr']:.4f}")

    print("\n=== per-category recall (categorised malicious skills) ===")
    by = collections.defaultdict(lambda: [0, 0])
    for r in mal:
        if r["category"]:
            by[r["category"]][1] += 1
            by[r["category"]][0] += bool(r["flagged"])
    for cat, (hit, tot) in sorted(by.items()):
        print(f"  {cat:<26} {hit}/{tot}  {hit/tot*100:.1f}%")
    if stubs:
        h = sum(1 for r in stubs if r["flagged"])
        held = sum(1 for r in stubs if r["our_state"] != "pass")
        print(f"\nuncategorised registry stubs: {len(stubs)}; flagged {h}, not passed {held} "
              f"(content is a name only, so passing them is the correct outcome)")

    print("\n=== our own three-state outcome (what the user actually gets) ===")
    held = sum(1 for r in mal if r["our_state"] in ("danger", "suspect"))
    print(f"  categorised malicious not published silently: {held}/{len(mal)} "
          f"(danger = rejected, suspect = held for human review)")
    for lab, group in (("malicious", mal), ("benign", ben), ("edge_case", edge), ("stub", stubs)):
        c = collections.Counter(r["our_state"] for r in group)
        print(f"  {lab:<10} {dict(sorted(c.items()))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    samples = load_samples()
    if args.limit:
        samples = samples[:args.limit]
    print(f"skill-type samples: {len(samples)}", flush=True)
    t0 = time.time()
    recs = []
    with open(OUT, "w") as fh, ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, rec in enumerate(ex.map(run_one, samples), 1):
            recs.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 25 == 0:
                print(f"{i}/{len(samples)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"=== done {len(recs)} in {time.time()-t0:.0f}s -> {OUT}")
    summarise(recs)


if __name__ == "__main__":
    main()
