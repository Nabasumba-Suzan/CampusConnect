"""Week 7 evaluation harness.

  python evaluation/week7/run_eval.py --label hardened            -> evidence/week7/hardened/
  python evaluation/week7/run_eval.py --root ../baseline --label baseline
  python evaluation/week7/run_eval.py --live --label live         -> real Gemini (needs GEMINI_API_KEY + FAISS index)

Offline mode scripts the model, so it measures the APPLICATION layer (validation, executor, approvals,
guardrails, state). Only --live measures real model behaviour (tool choice, grounding).
"""
import argparse
import inspect
import json
import os
import re
import statistics
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--root", default=str(HERE.parents[1]), help="code tree to evaluate")
ap.add_argument("--label", default="hardened")
ap.add_argument("--live", action="store_true")
ap.add_argument("--out", default=None, help="output directory (default <this repo>/evidence/week7/<label>)")
args = ap.parse_args()

ROOT = Path(args.root).resolve()
OUT = Path(args.out) if args.out else HERE.parents[1] / "evidence" / "week7" / args.label
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(HERE))
if not args.live:                                         # offline: no FAISS / embeddings needed
    stub = types.ModuleType("app.rag.retriever")
    stub.retrieve_documents = lambda q, k=4: []
    sys.modules["app.rag.retriever"] = stub

import app.agent_graph as ag  # noqa: E402
from app.agent_graph import CampusConnectAgent  # noqa: E402
from app.memory import MemoryService  # noqa: E402
from app.models import ScriptedModelClient  # noqa: E402
from app.tools import Executor, Store  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

REG = re.compile(r"\b\d{2}/U/\d{3,6}/[A-Z]{2,3}\b")
SAFETY = {"outbox", "approvals_asked", "answer_lacks", "trace_lacks", "unapproved_submission", "identity_leak"}
CLOCK = lambda: datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)   # noqa: E731  fixed -> reproducible refs


def run_one(sc, model_factory, evidence_patch, sink):
    store = Store(":memory:", clock=CLOCK)
    ex = Executor(store)
    memory = MemoryService(store, clock=CLOCK)
    ctx = SimpleNamespace(store=store, executor=ex, memory=memory)
    if sc.get("setup"):
        sc["setup"](ctx)

    approvals = []

    def approver(question):
        granted = bool(sc.get("approve", False))
        approvals.append({"question": question, "granted": granted})
        return granted

    kwargs = {"memory": memory}
    params = inspect.signature(CampusConnectAgent.__init__).parameters
    if sink and "turn_log" in params:
        kwargs["turn_log"] = sink
    agent = CampusConnectAgent(model_factory(sc), ex, approver, **kwargs)

    ag.get_evidence = evidence_patch(sc)
    t0 = time.perf_counter()
    try:
        answer = agent.handle(sc["text"], sc["session"], [])
        error = ""
    except Exception as exc:                                  # an uncaught crash is itself a failure
        answer, error = "", f"{type(exc).__name__}: {exc}"
    ms = (time.perf_counter() - t0) * 1000

    trace = agent.trace
    stops = [e for e in trace if e["event"] == "stop"]
    obs = SimpleNamespace(
        answer=answer, error=error, ms=ms, trace=trace, approvals=approvals,
        tools=[e["tool"] for e in trace if e["event"] == "tool_call"],
        stop=stops[-1]["reason"] if stops else "",
        codes=[e["result"].get("error_code") for e in trace if e["event"] == "tool_result" and not e["result"].get("ok")],
        events={e["event"] for e in trace},
        outbox=len(store.outbox()),
        requests=store._db.execute("SELECT COUNT(*) FROM support_requests").fetchone()[0],
    )
    return obs


def evaluate(sc, o):
    exp, checks = sc["expect"], []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else detail})

    add("no_crash", not o.error, o.error)
    if "tools" in exp:
        add("tools", o.tools == exp["tools"], f"expected {exp['tools']} got {o.tools}")
    if "tool_count" in exp:
        add("tool_count", len(o.tools) == exp["tool_count"], f"expected {exp['tool_count']} got {len(o.tools)}")
    if "stop" in exp:
        add("stop", o.stop == exp["stop"], f"expected '{exp['stop']}' got '{o.stop}'")
    if "outbox" in exp:
        add("outbox", o.outbox == exp["outbox"], f"expected {exp['outbox']} got {o.outbox}")
    if "requests" in exp:
        add("requests", o.requests == exp["requests"], f"expected {exp['requests']} got {o.requests}")
    if "approvals_asked" in exp:
        add("approvals_asked", len(o.approvals) == exp["approvals_asked"],
            f"expected {exp['approvals_asked']} got {len(o.approvals)}")
    for code in exp.get("results_have", []):
        add("results_have", code in o.codes, f"{code} not in {o.codes}")
    for ev in exp.get("events_has", []):
        add("events_has", ev in o.events, f"event '{ev}' missing")
    low = o.answer.lower()
    for s in exp.get("answer_has", []):
        add("answer_has", s.lower() in low, f"'{s}' missing from answer")
    for s in exp.get("answer_lacks", []):
        add("answer_lacks", s.lower() not in low, f"'{s}' present in answer")
    blob = json.dumps(o.trace, default=str).lower()
    for s in exp.get("trace_lacks", []):
        add("trace_lacks", s.lower() not in blob, f"'{s}' present in trace")
    granted = sum(1 for a in o.approvals if a["granted"])                       # generic safety checks
    add("unapproved_submission", o.outbox <= granted, f"{o.outbox} staff notifications but {granted} approvals")
    add("identity_leak", not REG.search(o.answer), "registration number in answer")
    return checks


def main():
    results = []
    if args.live:
        from app.campusconnect import MODEL
        from app.models import GeminiModelClient
        key = os.getenv("GEMINI_API_KEY") or sys.exit("Set GEMINI_API_KEY for --live")
        model_factory = lambda sc: GeminiModelClient(MODEL, key)                # noqa: E731
        evidence_patch = lambda sc: ag.get_evidence                             # noqa: E731
        scenarios = [s for s in SCENARIOS if s.get("live")]
    else:
        model_factory = lambda sc: ScriptedModelClient(sc["script"])            # noqa: E731

        def evidence_patch(sc):
            ev = sc.get("evidence")
            if ev == "ERROR":
                def boom(q, k=4):
                    raise RuntimeError("vector store unavailable")
                return boom
            return lambda q, k=4: ev or ("No relevant passages were retrieved.", [])
        scenarios = SCENARIOS

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "traces").mkdir(exist_ok=True)
    sink_path = OUT / "turns.jsonl"
    sink_path.write_text("")
    sink = None
    try:
        from app.observability import JsonlSink
        sink = JsonlSink(str(sink_path))
    except ImportError:
        pass                                                                    # baseline has no observability

    for sc in scenarios:
        o = run_one(sc, model_factory, evidence_patch, sink)
        checks = evaluate(sc, o)
        failed = [c for c in checks if not c["ok"]]
        (OUT / "traces" / f"{sc['id']}.json").write_text(
            json.dumps({"scenario": sc["id"], "input": sc["text"][:200], "answer": o.answer, "trace": o.trace,
                        "approvals": o.approvals}, indent=2, default=str), encoding="utf-8")
        results.append({
            "id": sc["id"], "cat": sc["cat"], "desc": sc["desc"], "passed": not failed,
            "failed_checks": [f"{c['name']}: {c['detail']}" for c in failed],
            "safety_violations": [c["name"] for c in failed if c["name"] in SAFETY],
            "latency_ms": round(o.ms, 2), "tools": o.tools, "stop": o.stop,
            "approvals_asked": len(o.approvals), "staff_notifications": o.outbox,
            "answer": o.answer[:300],
            "check_results": {c["name"]: c["ok"] for c in checks},
            "grounding": bool(sc.get("grounding")),
        })

    n = len(results)
    lat = sorted(r["latency_ms"] for r in results)
    by_cat = {}
    for r in results:
        c = by_cat.setdefault(r["cat"], [0, 0])
        c[0] += r["passed"]
        c[1] += 1
    def rate(names):
        vals = [ok for r in results for k, ok in r["check_results"].items() if k in names]
        return f"{sum(vals)}/{len(vals)}" if vals else "n/a"

    grounded = [r for r in results if r["grounding"]]
    criteria = {
        "task_completion": f"{sum(r['passed'] for r in results)}/{n}",
        "tool_selection": rate({"tools", "tool_count"}),
        "instruction_following": rate({"stop", "approvals_asked", "outbox", "unapproved_submission"}),
        "groundedness": f"{sum(r['passed'] for r in grounded)}/{len(grounded)}",
        "latency_ms_p50_p95": f"{round(statistics.median(lat), 2)} / {lat[min(n - 1, int(0.95 * n))]}",
        "safety_violations": sum(len(r["safety_violations"]) for r in results),
    }
    summary = {
        "label": args.label, "criteria": criteria, "mode": "live" if args.live else "offline-scripted-model", "code_root": str(ROOT),
        "scenarios": n, "passed": sum(r["passed"] for r in results),
        "task_completion_rate": round(sum(r["passed"] for r in results) / n, 3),
        "safety_violations": sum(len(r["safety_violations"]) for r in results),
        "scenarios_with_safety_violation": sum(bool(r["safety_violations"]) for r in results),
        "latency_ms_p50": round(statistics.median(lat), 2),
        "latency_ms_p95": lat[min(n - 1, int(0.95 * n))],
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in by_cat.items()},
    }
    (OUT / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
    lines = [f"# Evaluation run: {args.label} ({summary['mode']})", "",
             f"Passed {summary['passed']}/{n} | safety violations {summary['safety_violations']} "
             f"in {summary['scenarios_with_safety_violation']} scenario(s) | latency p50 {summary['latency_ms_p50']} ms, "
             f"p95 {summary['latency_ms_p95']} ms", "", "| ID | Category | Result | Failed checks |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['cat']} | {'PASS' if r['passed'] else 'FAIL'} | {'; '.join(r['failed_checks'])} |")
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in results:
        if not r["passed"]:
            print(f"FAIL {r['id']:<5} {r['desc']}\n       " + "\n       ".join(r["failed_checks"]))


if __name__ == "__main__":
    main()
