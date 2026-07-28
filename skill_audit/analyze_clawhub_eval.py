#!/usr/bin/env python3
import json, os, time
from collections import Counter

P = "/tmp/clawhub_eval.jsonl"
recs = [json.loads(l) for l in open(P, encoding="utf-8")]
print(f"file mtime: {time.ctime(os.path.getmtime(P))}   records: {len(recs)}")
print(f"our_state values: {dict(Counter(r['our_state'] for r in recs))}")
print(f"frontmatter provenance: {dict(Counter(r['frontmatter'] for r in recs))}")
ok = [r for r in recs if r["our_state"] in ("pass", "suspect", "danger")]
print(f"usable (non-error) records: {len(ok)}")

print("\n=== our verdict vs their clawscan label ===")
print(f"{'their':12} {'n':>4}  {'pass':>5} {'suspect':>8} {'danger':>7}   flagged%")
for lab in ("clean", "suspicious", "malicious"):
    sub = [r for r in ok if r["their_clawscan"] == lab]
    if not sub:
        continue
    c = Counter(r["our_state"] for r in sub)
    flagged = c["suspect"] + c["danger"]
    print(f"{lab:12} {len(sub):>4}  {c['pass']:>5} {c['suspect']:>8} {c['danger']:>7}   "
          f"{100*flagged/len(sub):.1f}%")

clean = [r for r in ok if r["their_clawscan"] == "clean"]
if clean:
    c = Counter(r["our_state"] for r in clean)
    print(f"\n--- on their 'clean' skills (n={len(clean)}) ---")
    print(f"  we say danger  : {c['danger']}  ({100*c['danger']/len(clean):.1f}%)  <- hard rejects")
    print(f"  we say suspect : {c['suspect']}  ({100*c['suspect']/len(clean):.1f}%)  <- review burden")
    nb = [r for r in clean if not r["has_bundle"]]
    cb = Counter(r["our_state"] for r in nb)
    print(f"  (no-bundle subset n={len(nb)}: danger={cb['danger']} suspect={cb['suspect']})")
    print("  categories driving flags on clean skills:")
    for k, v in Counter(cat for r in clean if r["our_state"] != "pass"
                        for cat in r["our_categories"]).most_common():
        print(f"    {k}: {v}")

print("\n=== finding categories overall ===")
for k, v in Counter(cat for r in ok for cat in r["our_categories"]).most_common():
    print(f"  {k}: {v}")

print("\n=== examples: their malicious, we passed ===")
for r in [r for r in ok if r["their_clawscan"] == "malicious" and r["our_state"] == "pass"][:8]:
    print(f"  {r['slug']}  (frontmatter={r['frontmatter']}, md_len={r['md_len']})")
print("\n=== examples: their clean, we said danger ===")
for r in [r for r in clean if r["our_state"] == "danger"][:8]:
    print(f"  {r['slug']}  cats={r['our_categories']}")
