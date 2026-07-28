#!/usr/bin/env python3
import json
from collections import Counter, defaultdict

recs = [json.loads(l) for l in open("/tmp/oasb_eval.jsonl", encoding="utf-8")]
print(f"records: {len(recs)}")
print(f"our_state values: {dict(Counter(r['our_state'] for r in recs))}")
print(f"frontmatter: {dict(Counter(r['frontmatter'] for r in recs))}")
print(f"artifact types: {dict(Counter(r['artifact_type'] for r in recs))}")
ok = [r for r in recs if r["our_state"] in ("pass", "suspect", "danger")]
print(f"usable: {len(ok)}")

DANGER_CATS = ["credential_exfiltration", "prompt_injection", "data_exfiltration", "heartbeat_rce"]
SUSPECT_CATS = ["persistence", "privilege_escalation"]

print("\n=== malicious, per OASB category (expected verdict declared before the run) ===")
print(f"{'category':26} {'expect':8} {'n':>3} {'pass':>5} {'suspect':>8} {'danger':>7}  {'flagged':>8}")
for cat in DANGER_CATS + SUSPECT_CATS + ["social_engineering"]:
    sub = [r for r in ok if r["category"] == cat]
    if not sub:
        continue
    c = Counter(r["our_state"] for r in sub)
    exp = "danger" if cat in DANGER_CATS else ("suspect" if cat in SUSPECT_CATS else "out-of-scope")
    flagged = c["danger"] + c["suspect"]
    print(f"{cat:26} {exp:8} {len(sub):>3} {c['pass']:>5} {c['suspect']:>8} {c['danger']:>7}  "
          f"{100*flagged/len(sub):>6.0f}%")

inscope = [r for r in ok if r["category"] in DANGER_CATS + SUSPECT_CATS]
flag = sum(1 for r in inscope if r["our_state"] != "pass")
hard = sum(1 for r in inscope if r["our_state"] == "danger")
print(f"\nin-scope malicious (n={len(inscope)}): flagged {flag} ({100*flag/len(inscope):.1f}%), "
      f"of which hard-rejected {hard} ({100*hard/len(inscope):.1f}%)")
dsub = [r for r in ok if r["category"] in DANGER_CATS]
dhard = sum(1 for r in dsub if r["our_state"] == "danger")
print(f"  red-line categories (n={len(dsub)}): hard-rejected {dhard} ({100*dhard/len(dsub):.1f}%), "
      f"flagged {sum(1 for r in dsub if r['our_state']!='pass')}")

ben = [r for r in ok if r["label"] == "benign"]
cb = Counter(r["our_state"] for r in ben)
print(f"\n=== benign (n={len(ben)}) ===")
print(f"  pass {cb['pass']}  suspect {cb['suspect']}  danger {cb['danger']}"
      f"   -> false-alarm {100*(cb['suspect']+cb['danger'])/len(ben):.1f}%, "
      f"wrong hard-reject {100*cb['danger']/len(ben):.1f}%")

edge = [r for r in ok if r["label"] == "edge_case"]
ce = Counter(r["our_state"] for r in edge)
print(f"\n=== edge_case (n={len(edge)}) — must NOT be hard-rejected ===")
print(f"  pass {ce['pass']}  suspect {ce['suspect']}  danger {ce['danger']}"
      f"   -> violated (danger) {ce['danger']}")
for r in edge:
    if r["our_state"] == "danger":
        print(f"    VIOLATION {r['id']} ({r['artifact_type']}, {r['source']}) cats={r['our_categories']}")

print("\n=== by artifact type: our flag rate ===")
for at in sorted({r["artifact_type"] for r in ok if r["artifact_type"]}):
    sub = [r for r in ok if r["artifact_type"] == at]
    mal = [r for r in sub if r["label"] == "malicious"]
    b = [r for r in sub if r["label"] == "benign"]
    fm = sum(1 for r in mal if r["our_state"] != "pass")
    fb = sum(1 for r in b if r["our_state"] != "pass")
    print(f"  {at:14} malicious {fm}/{len(mal)}  benign flagged {fb}/{len(b)}")

print("\n=== our finding categories on in-scope malicious ===")
for k, v in Counter(c for r in inscope for c in r["our_categories"]).most_common():
    print(f"  {k}: {v}")

print("\n=== misses on red-line categories (we passed) ===")
for r in [r for r in dsub if r["our_state"] == "pass"][:12]:
    print(f"  {r['id']:14} {r['category']:24} {r['artifact_type']:14} fm={r['frontmatter']}")
