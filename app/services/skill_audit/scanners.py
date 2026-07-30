"""Deterministic layer: secret detection/redaction + static skill scanning.

Two deterministic checks, complementing the semantic judge:
- ``detect-secrets`` (in-process): broad secret formats; masks before the judge.
- Cisco AI Defense ``skill-scanner`` (external CLI, no key): the judge's blind
  spots — bundled executable scripts, .pyc bytecode, unicode-hidden instructions.

A pure-regex redactor always runs so no raw live-format secret is ever sent to
the judge, regardless of the other tools.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from app.services.skill_audit.types import Finding

log = logging.getLogger("skill_audit.scanners")

# Common live-key formats. Placeholder guard: skip tvly-YOUR..., sk-YOUR...
_SECRET_PATS: list[tuple[str, re.Pattern]] = [
    ("tavily_key", re.compile(r"tvly-(?!YOUR)[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?(?!YOUR)[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def redact_secrets(text: str) -> tuple[str, list[Finding]]:
    """Mask any live-format secrets. Returns (redacted_text, findings)."""
    findings: list[Finding] = []
    redacted = text
    for name, pat in _SECRET_PATS:
        if pat.search(text):
            findings.append(
                Finding(
                    source="pipeline",
                    category="secret",
                    severity="critical",
                    evidence=f"<redacted:{name}>",
                    detail=f"live-format secret matched ({name})",
                )
            )
        redacted = pat.sub("<REDACTED_SECRET>", redacted)
    return redacted, findings


def detect_secrets_scan(text: str) -> tuple[list[Finding], str]:
    """Broad secret detection via detect-secrets (in-process).

    Returns (findings, redacted_text): findings for the audit, and the text with
    any detected secret values masked — so secrets in formats the regex layer
    doesn't cover (GitHub/GitLab/Slack/...) are NOT leaked to the semantic judge.

    Uses only the specific-FORMAT detectors (low false-positive). The
    high-entropy and keyword detectors are excluded: in per-line (ad-hoc) mode
    they fire on trivial text and would route almost every skill to review.
    Findings are MEDIUM (→ review); the high-confidence live-format keys are
    already red-line via ``redact_secrets``. Evidence is the detector name only.
    """
    try:
        from detect_secrets.core.scan import scan_line
        from detect_secrets.settings import transient_settings
    except ImportError:
        return [], text

    plugins = [
        {"name": n} for n in (
            "ArtifactoryDetector", "AWSKeyDetector", "AzureStorageKeyDetector",
            "BasicAuthDetector", "CloudantDetector", "DiscordBotTokenDetector",
            "GitHubTokenDetector", "GitLabTokenDetector", "IbmCloudIamDetector",
            "IbmCosHmacDetector", "JwtTokenDetector", "MailchimpDetector",
            "NpmDetector", "OpenAIDetector", "PrivateKeyDetector",
            "PypiTokenDetector", "SendGridDetector", "SlackDetector",
            "SoftlayerDetector", "SquareOAuthDetector", "StripeDetector",
            "TelegramBotTokenDetector", "TwilioKeyDetector",
        )
    ]

    findings: list[Finding] = []
    seen: set[str] = set()
    masked = text
    try:
        with transient_settings({"plugins_used": plugins}):
            for line in text.splitlines():
                for secret in scan_line(line):
                    val = getattr(secret, "secret_value", None)
                    if val:
                        masked = masked.replace(val, "<REDACTED_SECRET>")
                    kind = secret.type
                    if kind in seen:
                        continue
                    seen.add(kind)
                    findings.append(
                        Finding(
                            source="detect-secrets",
                            category="secret",
                            severity="medium",
                            evidence=f"<redacted:{kind}>",
                            detail=f"detect-secrets flagged {kind} (broad scan → review)",
                        )
                    )
    except Exception:  # noqa: BLE001 — never let the scanner crash the audit
        return findings, masked
    return findings, masked


# --- Cisco AI Defense skill-scanner (static analyzers, no key) --------------
# Only CRITICAL/HIGH/MEDIUM are considered; LOW/INFO dropped.
_CISCO_SEV = {"CRITICAL": "critical", "HIGH": "high", "MEDIUM": "medium"}

# Hygiene / policy rules that are NOT security threats. Mapping these would
# route every benign skill to review, so they are dropped.
_CISCO_IGNORE_RULE_PREFIX = ("MANIFEST_", "SOCIAL_ENG_")
_CISCO_IGNORE_RULES = {"FILE_MAGIC_MISMATCH", "LOW_ANALYZABILITY"}
_CISCO_IGNORE_CATS = {"policy_violation", "social_engineering"}


# Characters that genuinely hide or disguise content, which is what the
# hidden_instruction red line is about:
#   - zero width and word joiners: invisible, can hide text outright
#   - bidirectional controls: reorder what a reviewer sees
#   - Cyrillic / Greek letters and fullwidth Latin letters and digits: shaped
#     like ASCII, so they disguise identifiers and commands
# Ideographic scripts (Chinese, Japanese, Korean) are none of these: they are
# plainly visible and not confusable with ASCII letters, so Chinese comments and
# strings in bundled code are not hidden instructions. Fullwidth *punctuation*
# (，（）：；) is ordinary Chinese typography and is excluded for the same reason -
# only the fullwidth letters and digits can stand in for ASCII ones.
_HIDDEN_CHARS = re.compile(
    "["
    "\u200b-\u200f"      # zero width space / joiners / marks
    "\u2060-\u2064"      # word joiner, invisible operators
    "\ufeff"             # BOM used mid-text
    "\u202a-\u202e"      # bidi embedding / override
    "\u2066-\u2069"      # bidi isolates
    "\u0400-\u04ff"      # Cyrillic, confusable with Latin
    "\u0370-\u03ff"      # Greek, confusable with Latin
    "\uff10-\uff19"      # fullwidth digits
    "\uff21-\uff3a"      # fullwidth uppercase letters
    "\uff41-\uff5a"      # fullwidth lowercase letters
    "]"
)


def has_hidden_chars(text: str) -> bool:
    """True when the text contains characters that hide or disguise content."""
    return bool(_HIDDEN_CHARS.search(text or ""))


def _map_cisco(rule_id: str | None, category: str | None) -> str | None:
    """Map a Cisco finding to one of our categories, or None to drop it.

    Threat whitelist: only security-relevant findings map through; hygiene/
    policy noise (manifest, license, vague description, file-magic) is dropped.
    """
    rid = (rule_id or "").upper()
    cat = (category or "").lower()
    if cat in _CISCO_IGNORE_CATS or rid in _CISCO_IGNORE_RULES \
            or any(rid.startswith(p) for p in _CISCO_IGNORE_RULE_PREFIX):
        return None
    if "STEGANOGRAPH" in rid or "UNICODE" in rid:
        return "hidden_instruction"
    if "PROMPT_INJECTION" in rid or cat == "prompt_injection":
        return "prompt_injection"
    if ("COMMAND_INJECTION" in rid or "COMPOUND_FETCH" in rid or "FETCH_EXECUTE" in rid
            or cat in ("supply_chain", "tool_chaining_abuse", "privilege_escalation",
                       "tool_misuse")):
        return "dangerous_instruction"
    if "OBFUSCATION" in rid or cat == "obfuscation":
        return "hidden_instruction"
    if "BYTECODE" in rid or "BINARY" in rid or cat in ("bytecode", "malware"):
        return "malicious_artifact"
    # Unmapped but security-relevant (hygiene already dropped, LOW/INFO already
    # filtered): route to review conservatively, never a red-line auto-reject.
    return "malicious_artifact"


def cisco_scan(bin_path: str, target_dir: str) -> tuple[list[Finding], bool]:
    """Run the Cisco skill-scanner static analyzers (no key) over target_dir.

    Uses ``--policy strict`` (correct posture for untrusted uploads; the default
    ``balanced`` preset demotes dangerous patterns in SKILL.md as "instructional").
    Only security-relevant findings are mapped; hygiene/policy noise is dropped so
    benign skills are not falsely routed to review. Returns (findings, ran).
    """
    if not bin_path:
        return [], False
    try:
        proc = subprocess.run(
            [bin_path, "scan", target_dir, "--policy", "strict",
             "--use-behavioral", "--format", "json"],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return [], False
    if not (proc.stdout or "").strip():
        return [], False
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], False

    findings: list[Finding] = []
    for it in data.get("findings", []) or []:
        sev = _CISCO_SEV.get(str(it.get("severity", "")).upper())
        if sev is None:  # LOW / INFO -> drop
            continue
        cat = _map_cisco(it.get("rule_id"), it.get("category"))
        if cat is None:  # hygiene / policy noise -> drop
            continue
        # hidden_instruction is a red line, so a claim of hidden content has to
        # meet our definition of it before we hard-reject on it. Cisco reports
        # HOMOGLYPH_ATTACK for any non-ASCII script sitting next to ASCII, which
        # makes ordinary Chinese comments and strings in bundled code look like a
        # homoglyph attack. Read the file and drop the finding when nothing is
        # actually hidden or disguised.
        rid_up = str(it.get("rule_id") or "").upper()
        if cat == "hidden_instruction":
            rel = str(it.get("file_path") or "")
            target = (Path(target_dir) / rel).resolve() if rel else None
            if target and str(target).startswith(str(Path(target_dir).resolve())) \
                    and target.is_file():
                try:
                    content = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = ""
                if not has_hidden_chars(content):
                    log.info("cisco %s on %s: visible, non-confusable characters "
                             "only - not hidden content, dropped", rid_up, rel)
                    continue
        # The static layer detects prompt injection by matching coercive phrases
        # ("do not tell the user", "tell the user what to approve"), which also
        # occur in legitimate skills that brief the user or manage credentials.
        # Structural evidence (hidden unicode, shipped executables) stays a red
        # line; a phrase match alone only routes to human review, and the
        # semantic judge remains the arbiter of instruction-layer intent.
        if cat == "prompt_injection" and sev in ("critical", "high"):
            sev = "medium"
        title = (it.get("title") or it.get("rule_id") or "")[:200]
        loc = it.get("file_path") or ""
        findings.append(
            Finding(
                source="cisco",
                category=cat,
                severity=sev,
                evidence=(f"{loc}: {title}".strip(": ") or None),
                detail=(it.get("description") or "")[:500] or None,
            )
        )
    return findings, True


# Extensions that indicate shipped code/executables (non-red-line -> review).
_CODE_EXT = {
    ".sh", ".bash", ".zsh", ".py", ".rb", ".pl", ".ps1", ".bat", ".cmd",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".jar", ".class",
}


# Compiled or binary payloads: a reviewer cannot read these, so shipping one
# defeats the human review the suspect state exists for. Source scripts are not
# listed here - the static analyser judges those on content, and routing every
# skill that ships a helper script to review would bury the reviewers.
_UNREADABLE_EXT = {
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib", ".bin", ".jar", ".class",
    ".o", ".a", ".wasm", ".node",
}


def unreadable_artifact_scan(target_dir: str) -> list[Finding]:
    """Flag bundled compiled/binary files so they go to human review.

    Runs independently of the static analyser: that tool classifies shipped
    bytecode as a hygiene issue, but for an audit whose fallback is a person
    reading the diff, an unreadable file is a review-worthy fact.
    """
    findings: list[Finding] = []
    root = Path(target_dir)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _UNREADABLE_EXT:
            continue
        findings.append(
            Finding(
                source="pipeline",
                category="malicious_artifact",
                severity="medium",
                evidence=str(path.relative_to(root)),
                detail="Bundles a compiled or binary file, which cannot be reviewed "
                       "by reading it.",
            )
        )
    return findings


def artifact_scan(target_dir: str) -> list[Finding]:
    """Flag shipped executables/scripts as malicious_artifact (medium, non-red-line).

    Fallback used only when the Cisco scanner is unavailable; a cheap extension/
    shebang heuristic so bundled executables still route to review.
    """

    findings: list[Finding] = []
    for p in Path(target_dir).rglob("*"):
        if not p.is_file() or p.name in ("SKILL.md", "manifest.json"):
            continue
        ext = p.suffix.lower()
        reason = None
        if ext in _CODE_EXT:
            reason = f"executable/script artifact ({ext})"
        else:
            try:
                if p.read_bytes()[:2] == b"#!":
                    reason = "shebang script artifact"
            except OSError:
                pass
        if reason:
            findings.append(
                Finding("pipeline", "malicious_artifact", "medium",
                        evidence=p.name, detail=reason)
            )
    return findings
