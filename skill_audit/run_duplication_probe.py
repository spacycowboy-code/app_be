#!/usr/bin/env python3
"""Measure text overlap between the skills currently in the catalogue.

The duplication dimension is not implemented. This probe exists so the numbers in
`.kiro/specs/skill-audit/duplication.md` can be reproduced: it computes exact
Jaccard overlap over word shingles for every pair, from two views.

    problem view : name + description  - "what does it solve"
    method view  : SKILL.md body       - "how does it solve it"

Exact Jaccard, not MinHash: at catalogue scale every pair can be compared
directly, so the probe measures the quantity itself rather than an estimate of it.
MinHash only becomes necessary when the pair count stops being tractable.

    python3 skill_audit/run_duplication_probe.py [--threshold 0.30]
"""
import argparse, itertools, json, os, re, urllib.request
from pathlib import Path

API = os.environ.get("SKILL_API", "http://localhost:9100/api/skills")
OUT = os.environ.get("DUP_OUT", "/tmp/duplication_probe.jsonl")
SHINGLE = 4          # words per shingle


def get(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def normalise(text: str) -> list[str]:
    """Lowercase, drop punctuation, split. CJK has no spaces, so each character
    is treated as a token; that makes the two scripts comparable in the same
    shingle scheme."""
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").lower())
    out: list[str] = []
    for token in text.split():
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            out += list(token)
        else:
            out.append(token)
    return out


def shingles(tokens: list[str], n: int = SHINGLE) -> set[str]:
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end > 0:
            return md[end + 4:]
    return md


def selftest(skills):
    """Positive control: how far does text overlap survive editing?

    Variants are derived from a real catalogue skill, deterministically, so the
    sensitivity numbers can be reproduced. Paraphrase is measured against an
    authored fixture pair (two skills we wrote for the same task in different
    words) because a paraphrase cannot be generated mechanically.
    """
    # Edits are defined on sentence-ish blocks, not on lines, so the numbers do
    # not depend on how the author happened to wrap the text.
    base = max(skills, key=lambda s: len(re.split(r"[\n。;.!?]+", s["_body"])))
    body = base["_body"]
    blocks = [b for b in re.split(r"(?<=[\n。;.!?])", body) if b.strip()]
    kept = "".join(b for i, b in enumerate(blocks) if i % 5 != 0)        # drop 20%
    reordered = "".join(blocks[::-1])
    variants = {
        "verbatim copy": body,
        "same body, new name": body,
        "20% of blocks removed": kept,
        "blocks reordered": reordered,
    }
    print(f"\n=== positive control, derived from: {base['name']} ===")
    for label, text in variants.items():
        m = jaccard(base["method"], shingles(normalise(text)))
        print(f"  method-view overlap {m:.2f}   {label}")
    fx = Path(__file__).with_name("dup_fixtures")
    a, b = fx / "paraphrase-a.md", fx / "paraphrase-b.md"
    if a.is_file() and b.is_file():
        ta = shingles(normalise(strip_frontmatter(a.read_text(encoding="utf-8"))))
        tb = shingles(normalise(strip_frontmatter(b.read_text(encoding="utf-8"))))
        pa = shingles(normalise(a.read_text(encoding="utf-8").split("---")[1]), 3)
        pb = shingles(normalise(b.read_text(encoding="utf-8").split("---")[1]), 3)
        print(f"  method-view overlap {jaccard(ta, tb):.2f}   authored paraphrase pair "
              f"(same task, different words); problem view {jaccard(pa, pb):.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.30)
    ap.add_argument("--selftest", action="store_true",
                    help="also measure how far overlap survives editing")
    args = ap.parse_args()

    listing = get(f"{API}?limit=500")
    items = listing if isinstance(listing, list) else listing.get("items", [])
    skills = []
    for it in items:
        sid = it.get("id")
        try:
            body = strip_frontmatter(get_text(f"{API}/{sid}/markdown"))
        except Exception as exc:
            print(f"  skip {sid}: {exc}")
            continue
        problem = f"{it.get('name', '')} {it.get('description', '')}"
        skills.append({
            "id": sid,
            "name": it.get("name"),
            "_body": body,
            "problem": shingles(normalise(problem), 3),
            "method": shingles(normalise(body)),
            "body_len": len(body),
        })
    print(f"skills compared: {len(skills)}  (pairs: {len(skills)*(len(skills)-1)//2})")

    pairs = []
    for a, b in itertools.combinations(skills, 2):
        p = jaccard(a["problem"], b["problem"])
        m = jaccard(a["method"], b["method"])
        pairs.append({"a": a["id"], "b": b["id"], "a_name": a["name"], "b_name": b["name"],
                      "problem": round(p, 4), "method": round(m, 4)})
    pairs.sort(key=lambda x: -max(x["problem"], x["method"]))
    with open(OUT, "w") as fh:
        for rec in pairs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    over = [p for p in pairs if max(p["problem"], p["method"]) >= args.threshold]
    print(f"\npairs at or above {args.threshold}: {len(over)}")
    for p in over:
        print(f"  problem {p['problem']:.2f}  method {p['method']:.2f}  "
              f"{p['a_name']}  <->  {p['b_name']}")
    print("\ntop 8 pairs regardless of threshold:")
    for p in pairs[:8]:
        print(f"  problem {p['problem']:.2f}  method {p['method']:.2f}  "
              f"{p['a_name']}  <->  {p['b_name']}")
    if pairs:
        mp = max(p["problem"] for p in pairs)
        mm = max(p["method"] for p in pairs)
        print(f"\nhighest problem-view overlap: {mp:.2f}; highest method-view: {mm:.2f}")
    print(f"all pairs written to {OUT}")
    if args.selftest and skills:
        selftest(skills)


if __name__ == "__main__":
    main()
