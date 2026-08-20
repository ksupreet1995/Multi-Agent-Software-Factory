"""On-demand evaluation runner for the web UI.

Calls the real AgentCore Evaluate API against the deployed director's recent
traces and returns per-evaluator scores + per-session explanations, so the UI
can render them visually instead of via CLI JSON.

Uses the AgentCore Evaluations SDK if available; otherwise shells out to the
`agentcore run eval` CLI as a fallback.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from webui.core import CONFIG

_DEPLOY_DIR = Path(__file__).parent.parent / "deploy_agentcore" / CONFIG.deploy_project_dir

# Evaluators surfaced in the UI: AWS-managed built-ins + BOTH of our custom
# evaluators — report_quality (LLM-as-a-judge, subjective quality) and
# report_schema_check (code-based Lambda, deterministic field-presence, no LLM).
# Showing both makes the "LLM judge vs code-based" story concrete in the UI.
UI_EVALUATORS = [
    "Builtin.GoalSuccessRate",
    "Builtin.Helpfulness",
    "Builtin.ToolSelectionAccuracy",
    "report_quality",
    "report_schema_check",
]


def is_available() -> bool:
    return _DEPLOY_DIR.exists()


def _latest_session_id() -> str | None:
    """Find the most recent director session id from CloudWatch.

    Scoping the on-demand eval to a single recent session keeps it fast — the
    LLM-judge cost scales with session count, and evaluating a whole day of
    accumulated demo sessions is what causes timeouts.
    """
    try:
        import boto3

        arn = _director_arn()
        if not arn:
            return None
        rid = arn.split("/")[-1]
        logs = boto3.client("logs", region_name=CONFIG.aws_region)
        group = f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
        streams = logs.describe_log_streams(
            logGroupName=group, orderBy="LastEventTime", descending=True, limit=5
        )
        for s in streams.get("logStreams", []):
            # runtime session id is embedded in trace attributes; recover from
            # the most recent stream's events.
            ev = logs.get_log_events(
                logGroupName=group, logStreamName=s["logStreamName"],
                limit=50, startFromHead=False,
            )
            for e in ev.get("events", []):
                m = re.search(r'"session\.id"\s*:\s*"([^"]{16,})"', e.get("message", ""))
                if m:
                    return m.group(1)
    except Exception:  # noqa: BLE001
        return None
    return None


def _director_arn() -> str | None:
    state = _DEPLOY_DIR / "agentcore" / ".cli" / "deployed-state.json"
    try:
        data = json.loads(state.read_text())
        return data["targets"]["default"]["resources"]["runtimes"]["director"]["runtimeArn"]
    except Exception:  # noqa: BLE001
        return None


def run_ondemand(evaluators: list[str], days: int = 1, session_id: str | None = None) -> dict:
    """Run an on-demand eval via the agentcore CLI and normalize the result.

    Defaults to the single most recent session so the eval stays fast enough
    for a live demo. Pass session_id explicitly to override.
    """
    target_session = session_id or _latest_session_id()

    cmd = ["agentcore", "run", "eval", "--runtime", "director",
           "--evaluator", *evaluators, "--json"]
    if target_session:
        cmd += ["--session-id", target_session]
    else:
        # Fall back to a short window if we couldn't resolve a session.
        cmd += ["--days", str(days)]

    try:
        proc = subprocess.run(
            cmd, cwd=str(_DEPLOY_DIR), capture_output=True, text=True,
            timeout=300, shell=True,
        )
    except subprocess.TimeoutExpired:
        return {"error": "evaluation timed out (try again — scored session may be large)"}

    raw = proc.stdout.strip()
    data = _extract_json(raw)
    if not data:
        return {"error": "no JSON in eval output", "stderr": proc.stderr[-400:]}
    result = _normalize(data)
    result["scoped_session"] = target_session or "(recent window)"
    return result


def _extract_json(text: str) -> dict | None:
    text = text.lstrip("\ufeff").strip()
    start = text.find("{")
    if start == -1:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        # find last complete object
        try:
            return json.loads(text[start:text.rfind("}") + 1])
        except json.JSONDecodeError:
            return None


def _normalize(data: dict) -> dict:
    run = data.get("run", data)
    results = []
    for r in run.get("results", []):
        sessions = [
            {
                "session_id": s.get("sessionId", ""),
                "value": s.get("value"),
                "label": s.get("label", ""),
                "explanation": (s.get("explanation", "") or "").strip(),
            }
            for s in r.get("sessionScores", [])
        ]
        results.append({
            "evaluator": r.get("evaluator", ""),
            "aggregate": r.get("aggregateScore"),
            "sessions": sessions,
        })
    return {
        "agent": run.get("agent", "director"),
        "session_count": run.get("sessionCount", 0),
        "evaluators": run.get("evaluators", []),
        "results": results,
        "region": CONFIG.aws_region,
    }
