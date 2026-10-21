"""Structured turn log (Week 7): one JSON line per agent turn, built from the trace.

Logged: ids, pseudonymous student hash, model, prompt id, timings, retrieval, tool outcomes, guardrail
and approval events, stop reason. NOT logged: the student's message text or the model's answer
(only length and a short hash), so the log can be shared for debugging without exposing content.
"""

import hashlib
import json
from datetime import datetime, timezone


def _h(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:10]


class JsonlSink:
    def __init__(self, path: str):
        self.path = path

    def write(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def summarise_turn(trace: list, *, trace_id: str, model: str, prompt_id: str, student_id, user_text: str,
                   total_ms: float) -> dict:
    ev = lambda name: [e for e in trace if e["event"] == name]   # noqa: E731
    stops, retrieved = ev("stop"), ev("retrieved_evidence")
    workflow = ev("workflow_state")
    return {
        "trace_id": trace_id,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "student": _h(student_id) if student_id else None,
        "model": model,
        "prompt_id": prompt_id,
        "user_text_len": len(user_text or ""),
        "user_text_hash": _h(user_text),
        "retrieval": {
            "ms": retrieved[0].get("ms") if retrieved else None,
            "sources": len(retrieved[0].get("sources", [])) if retrieved else 0,
            "error": bool(ev("retrieval_error")),
        },
        "model_calls": [{"iteration": e["iteration"], "ms": e.get("ms"), "requested": e["requested_tools"]}
                        for e in ev("model_decision")],
        "tool_calls": [{"tool": e["tool"], "ms": e.get("ms"),
                        "outcome": "OK" if e["result"].get("ok") else e["result"].get("error_code")}
                       for e in ev("tool_result")],
        "approvals": [{"granted": e["granted"]} for e in ev("approval")],
        "guardrails": [{"rule": e["rule"], "action": e["action"]} for e in ev("guardrail")],
        "memory": {"read": bool(ev("memory_read")), "write": bool(ev("memory_write"))},
        "stop_reason": stops[-1]["reason"] if stops else None,
        "phase": workflow[-1]["phase"] if workflow else None,
        "total_ms": round(total_ms, 2),
    }
