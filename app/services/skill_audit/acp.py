"""ACP client for the local kiro agent, plus the bits needed to look like a chat API.

The skill scanner drives its semantic and meta analyzers through an OpenAI-compatible
endpoint. There is no hosted provider key in this environment, but kiro-cli is logged in
and speaks ACP over stdio, so this translates between the two. The scanner keeps its own
orchestration, prompts and taxonomy; the model stays a subordinate component with no
tools at all.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field

# Zed's Agent Client Protocol: JSON-RPC over stdio.
_PROTOCOL_VERSION = 1


class AcpError(RuntimeError):
    """The agent could not be driven to an answer. Callers must fail closed."""


def render_prompt(messages: list[dict]) -> str:
    """Flatten a chat request into one prompt.

    The scanner's own prompts already carry the untrusted-input delimiters and the
    output schema, so nothing is added here beyond role labels when there is more
    than one message.
    """
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # OpenAI content parts
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        if not content:
            continue
        role = m.get("role", "user")
        parts.append(str(content) if len(messages) == 1 else f"[{role}]\n{content}")
    return "\n\n".join(parts)


def extract_json(text: str) -> str:
    """Return just the JSON object from a chat reply.

    The scanner asks for strict JSON; a chat model answers like a chat model — it fences
    the block and sometimes appends a sentence of explanation. The scanner then fails to
    parse and reports LLM_ANALYSIS_FAILED, which the gate reads as "this layer did not
    run": the audit degrades for a packaging reason. Unwrapping belongs here because it
    is protocol translation, not a change of verdict.
    """
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    start = body.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(body[start:], start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return body[start:i + 1]
    return body[start:]          # unbalanced; hand it back and let the caller fail loudly


def validate_model(bin_path: str, model: str, timeout: int = 60) -> None:
    """Fail fast on an unknown model name.

    ``kiro-cli acp --model <typo>`` accepts the name silently and serves whatever the
    default is, while ``kiro-cli chat --model <typo>`` rejects it and lists the valid
    values. Without this check a typo would silently change which model judges skills,
    and the audit record's attribution to a model would be false.
    """
    if not model:
        return
    proc = subprocess.run(
        [bin_path, "chat", "--no-interactive", "--trust-tools=", "--model", model],
        input="ok", capture_output=True, text=True, timeout=timeout,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    if "does not exist" in combined:
        raise AcpError(f"unknown model {model!r}: {combined.strip()[:300]}")


@dataclass
class AcpSession:
    """One conversation: spawn, initialize, one prompt, done.

    A fresh process per request: one audit must not see anything from another audit.
    """

    bin_path: str
    model: str = ""
    agent: str = ""
    cwd: str = "/tmp"
    timeout: float = 600.0
    proc: subprocess.Popen | None = field(default=None, init=False)
    _id: int = field(default=0, init=False)
    session_id: str | None = field(default=None, init=False)

    def _start(self) -> None:
        cmd = [self.bin_path, "acp", "--trust-tools="]
        if self.agent:
            cmd += ["--agent", self.agent]
        if self.model:
            cmd += ["--model", self.model]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=self.cwd,
            start_new_session=True,
        )

    def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict, deadline: float) -> tuple[dict, str]:
        """Send a request and pump notifications until its reply arrives.

        Permission requests are denied rather than ignored: a judge that wants a tool is
        not doing the job we asked for, and an unanswered request would hang.
        """
        assert self.proc and self.proc.stdout
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        chunks: list[str] = []
        while True:
            if time.monotonic() > deadline:
                raise AcpError(f"{method}: timed out")
            line = self.proc.stdout.readline()
            if not line:
                raise AcpError(f"{method}: agent closed the stream")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise AcpError(f"{method}: {msg['error']}")
                return msg.get("result") or {}, "".join(chunks)
            if msg.get("method") == "session/request_permission" and msg.get("id") is not None:
                options = (msg.get("params") or {}).get("options") or []
                reject = next((o for o in options if "reject" in str(o.get("kind", ""))), None)
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "result": {"optionId": reject["optionId"]} if reject
                            else {"outcome": {"outcome": "cancelled"}}})
                continue
            update = (msg.get("params") or {}).get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                chunks.append((update.get("content") or {}).get("text") or "")

    def complete(self, prompt: str) -> str:
        deadline = time.monotonic() + self.timeout
        self._start()
        self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {"name": "rp-ai-skill-audit", "version": "1"},
        }, deadline)
        result, _ = self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, deadline)
        self.session_id = result.get("sessionId")
        _, text = self._request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": prompt}],
        }, deadline)
        return text

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass


def complete_json(*, bin_path: str, model: str, agent: str, cwd: str, timeout: float,
                  prompt: str, attempts: int = 2) -> str:
    """Get one JSON object out of the agent, retrying once if it answers in prose.

    A chat model occasionally answers a JSON request with a markdown report. Retrying
    once with the requirement restated recovers it; a second failure is reported as-is so
    the caller can fail closed rather than pretend the layer ran.
    """
    text = ""
    for attempt in range(attempts):
        ask = prompt if attempt == 0 else (
            prompt + "\n\nYour previous reply was not valid JSON. Output exactly one JSON "
            "object per the schema above, starting with { and ending with }, and nothing else.")
        session = AcpSession(bin_path=bin_path, model=model, agent=agent, cwd=cwd, timeout=timeout)
        try:
            text = extract_json(session.complete(ask))
        finally:
            session.close()
        if text.startswith("{"):
            return text
    return text
