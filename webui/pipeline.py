"""Strands Evals -> AgentCore Evals pipeline, streamed for the web UI.

Yields progress events for each stage so the Evals tab can visualize the
two-tool loop:
  1. AUTHOR scenarios as Strands Evals Case objects (with ground truth)
  2. RUN each through the deployed AgentCore director (real CloudWatch sessions)
  3. SCORE with an AgentCore batch evaluation using the Strands ground truth

Each yielded dict is a UI event: {stage, status, detail, data}.
"""

from __future__ import annotations

import json
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Iterator

from webui.core import CONFIG

_STATE = (
    Path(__file__).parent.parent
    / "deploy_agentcore" / CONFIG.deploy_project_dir / "agentcore" / ".cli" / "deployed-state.json"
)
_TRAJ = [
    "get_crm_customers", "delegate_to_code_writer",
    "delegate_to_code_reviewer", "execute_python",
]


def _director_arn() -> str | None:
    try:
        data = json.loads(_STATE.read_text())
        return data["targets"]["default"]["resources"]["runtimes"]["director"]["runtimeArn"]
    except Exception:  # noqa: BLE001
        return None


def is_available() -> bool:
    return _director_arn() is not None


def _ev(stage: str, status: str, detail: str, data: dict | None = None) -> dict:
    return {"stage": stage, "status": status, "detail": detail, "data": data or {}}


def run_pipeline() -> Iterator[dict]:
    import boto3

    arn = _director_arn()
    if not arn:
        yield _ev("author", "error", "Deployed director not found.")
        return

    # --- 1. AUTHOR (Strands Evals) ---------------------------------------
    yield _ev("author", "running", "Authoring scenarios with Strands Evals Cases…")
    try:
        from strands_evals import Case
    except ImportError:
        yield _ev("author", "error", "strands-agents-evals not installed.")
        return

    # Distinct scenarios — each tests a different capability, with its OWN
    # ground truth (expected assertion + expected tool trajectory). This is
    # what scenario authoring is for: varied, targeted test cases, not copies.
    scenarios = [
        {
            "tenant": "msp-a",
            "input": "Generate a full customer status report for tenant msp-a",
            "assertion": (
                "Reports total MRR of $20,800 across 4 customers, 11 open support "
                "tickets, and flags Fabrikam Inc as the at-risk account."
            ),
            "trajectory": _TRAJ,
        },
        {
            "tenant": "msp-b",
            "input": "For tenant msp-b, list ONLY the at-risk accounts and their open ticket counts",
            "assertion": (
                "Identifies Proseware Systems as at-risk with 9 open tickets and "
                "does not include healthy/active accounts in the at-risk list."
            ),
            "trajectory": _TRAJ,
        },
        {
            "tenant": "msp-a",
            "input": "For tenant msp-a, which single customer should we prioritize and why?",
            "assertion": (
                "Recommends prioritizing Fabrikam Inc, justified by its at-risk "
                "status and high open-ticket count relative to its MRR."
            ),
            "trajectory": _TRAJ,
        },
    ]
    cases = [
        Case(
            name=f"case-{s['tenant']}-{uuid.uuid4().hex[:6]}",
            input=s["input"],
            expected_assertion=s["assertion"],
            expected_trajectory=s["trajectory"],
            metadata={"tenant": s["tenant"]},
        )
        for s in scenarios
    ]
    yield _ev("author", "done", f"Authored {len(cases)} distinct Strands Cases with ground truth",
              {"cases": [{"name": c.name, "input": c.input,
                          "tenant": c.metadata["tenant"],
                          "assertion": c.expected_assertion,
                          "trajectory": c.expected_trajectory} for c in cases]})

    # --- 2. RUN through the deployed director ----------------------------
    yield _ev("run", "running", "Invoking the deployed AgentCore director per scenario…")
    client = boto3.client("bedrock-agentcore", region_name=CONFIG.aws_region)
    sessions = []
    for c in cases:
        tenant = c.metadata["tenant"]
        sid = f"uipipeline-{tenant}-{uuid.uuid4().hex}"
        payload = json.dumps({"prompt": c.input, "tenantId": tenant,
                              "userId": "eval-bot"}).encode()
        try:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=arn, runtimeSessionId=sid, payload=BytesIO(payload)
            )
            output_text = _drain_response_text(resp)
            sessions.append({"sessionId": sid, "assertions": [c.expected_assertion],
                             "expectedTrajectory": c.expected_trajectory,
                             "input": c.input, "output": output_text})
            yield _ev("run", "running", f"Ran {c.name} → session created", {"session": sid})
        except Exception as exc:  # noqa: BLE001
            yield _ev("run", "error", f"Invocation failed: {exc}")
            return
    yield _ev("run", "done", f"Produced {len(sessions)} sessions with traces in CloudWatch",
              {"sessions": [s["sessionId"] for s in sessions]})

    # --- 3. SCORE with AgentCore batch evaluation ------------------------
    yield _ev("score", "running", "Waiting ~30s for traces to index, then scoring…")
    time.sleep(30)

    gt = {"sessionMetadata": [
        {"sessionId": s["sessionId"], "assertions": s["assertions"],
         "expectedTrajectory": s["expectedTrajectory"]}
        for s in sessions
    ]}
    gt_path = Path(__file__).parent.parent / "deploy_agentcore" / CONFIG.deploy_project_dir / "_ui_pipeline_gt.json"
    gt_path.write_text(json.dumps(gt))

    result = _run_batch(gt_path)
    if result.get("error"):
        yield _ev("score", "error", result["error"])
        return
    yield _ev("score", "done",
              f"AgentCore batch eval complete over {result.get('session_count', 0)} sessions",
              {"results": result.get("results", [])})

    # --- 4. Strands Evals LOCAL PRE-DEPLOY GATE --------------------------
    # This is the inner-loop role: Strands runs a local pass/fail check on the
    # captured outputs against the same rubric BEFORE you'd promote a change.
    # It is NOT a competing score against AgentCore — it's the dev-time gate
    # that decides "is this good enough to ship / keep monitoring in prod?".
    yield _ev("strands_gate", "running",
              "Strands Evals: local pre-deploy gate (pass/fail on the same outputs)…")
    gate = _run_strands_gate(sessions)
    if gate.get("error"):
        yield _ev("strands_gate", "error", gate["error"])
        return
    yield _ev("strands_gate", "done", gate["summary"], {"gate": gate})


def _drain_response_text(resp) -> str:
    """Accumulate the director's streamed text output."""
    text = []
    for chunk in resp.get("response", []):
        raw = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = (ev.get("event", {}).get("contentBlockDelta", {})
                 .get("delta", {}).get("text"))
            if d:
                text.append(d)
    return "".join(text).strip()


def _run_strands_gate(sessions: list[dict], threshold: float = 0.7) -> dict:
    """Strands Evals as a local PRE-DEPLOY GATE (inner loop).

    Runs Strands' OutputEvaluator on each captured output against the report
    rubric, then returns a pass/fail verdict per scenario. This is the dev-time
    quality gate — the role Strands plays before AgentCore monitors in prod.
    It intentionally does not present a headline "score" competing with the
    managed service; it answers "would this pass the gate?".
    """
    try:
        from strands_evals.evaluators import OutputEvaluator
        from strands_evals.types.evaluation import EvaluationData
    except ImportError:
        return {"error": "strands-agents-evals not installed"}

    try:
        cases = []
        for s in sessions:
            if not s.get("output"):
                continue
            # Use THIS scenario's authored ground truth as the rubric, so the
            # gate checks the specific expected result, not a generic one.
            assertion = (s.get("assertions") or [""])[0]
            rubric = (
                f"Pass if the output satisfies this expected result: {assertion} "
                f"Score 0-1 where 1 means the expected result is fully met."
            )
            data = EvaluationData(input=s.get("input", ""), actual_output=s["output"])
            ev = OutputEvaluator(rubric=rubric)
            outputs = ev.evaluate(data)
            score = float(outputs[0].score) if outputs and outputs[0].score is not None else 0.0
            reason = (outputs[0].reason if outputs else "") or ""
            cases.append({
                "scenario": s.get("input", "")[:60],
                "score": round(score, 2),
                "passed": score >= threshold,
                "reason": reason.strip(),
            })
        passed = sum(1 for c in cases if c["passed"])
        total = len(cases)
        verdict = "PASS" if passed == total and total > 0 else "REVIEW"
        summary = (
            f"Pre-deploy gate: {passed}/{total} scenarios passed "
            f"(threshold {threshold:.0%}) → {verdict}"
        )
        return {"verdict": verdict, "passed": passed, "total": total,
                "threshold": threshold, "cases": cases, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Strands gate failed: {exc}"}


def _cleanup_old_batches() -> None:
    """Delete previous ui_pipeline_* batch evaluations to avoid console clutter."""
    try:
        import boto3
        c = boto3.client("bedrock-agentcore", region_name=CONFIG.aws_region)
        r = c.list_batch_evaluations(maxResults=50)
        key = next((k for k in r if isinstance(r[k], list)), None)
        for it in r.get(key, []):
            name = (it.get("name") or "")
            bid = it.get("batchEvaluationId") or it.get("id")
            if bid and (name.startswith("ui_pipeline_") or not name):
                try:
                    c.delete_batch_evaluation(batchEvaluationId=bid)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def _run_batch(gt_path: Path) -> dict:
    import subprocess
    import re

    # Clean up prior pipeline batches so the console shows just the latest run,
    # then create a fresh one. Same evaluator set as the on-demand path.
    _cleanup_old_batches()
    name = f"ui_pipeline_{uuid.uuid4().hex[:8]}"
    cmd = [
        "agentcore", "run", "batch-evaluation", "--runtime", "director",
        "--evaluator", "Builtin.GoalSuccessRate", "Builtin.ToolSelectionAccuracy",
        "Builtin.Helpfulness", "report_quality",
        "--ground-truth", gt_path.name, "--name", name, "--wait",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(gt_path.parent), capture_output=True, text=True,
            timeout=900, shell=True,
        )
    except subprocess.TimeoutExpired:
        return {"error": "batch evaluation timed out"}
    finally:
        try:
            gt_path.unlink()
        except OSError:
            pass

    out = proc.stdout
    # Parse lines like: "  Builtin.GoalSuccessRate: 1.00 avg [3 evaluated]"
    # or custom: "  report_quality: 4.00 avg [3 evaluated]"
    results = []
    for m in re.finditer(r"([\w.]+):\s*([\d.]+)\s*avg\s*\[(\d+)\s*evaluated\]", out):
        results.append({"evaluator": m.group(1), "aggregate": float(m.group(2)),
                        "count": int(m.group(3))})
    sc = re.search(r"(\d+)\s+sessions?,\s+(\d+)\s+completed", out)
    session_count = int(sc.group(1)) if sc else len(results)
    if not results:
        return {"error": "no batch results parsed", "raw": out[-400:]}
    return {"results": results, "session_count": session_count}
