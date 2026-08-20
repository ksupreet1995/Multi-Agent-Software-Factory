"""Strands Evals  +  AgentCore Evals — the two-tool pipeline.

Demonstrates how the framework-level eval SDK (Strands Evals) and the managed
eval service (AgentCore Evaluations) compose, using the same OTEL traces as the
shared substrate:

  1. AUTHOR  — use Strands Evals `Case` objects (optionally driven by its
     UserSimulator) to define realistic scenarios + ground truth
     (expected assertion, expected tool trajectory). This is the "inner loop"
     / dev-time harness.
  2. RUN     — invoke the DEPLOYED AgentCore director runtime for each scenario,
     producing real traces in CloudWatch (one session per scenario).
  3. SCORE   — submit an AgentCore BATCH EVALUATION over those sessions with the
     ground truth carried from the Strands Cases. This is the "outer loop" /
     managed service scoring at scale.

Net: Strands Evals generates + structures; AgentCore Evals scores + monitors.

Usage:
    python strands_agentcore_pipeline.py            # full pipeline
    python strands_agentcore_pipeline.py --no-batch # author + run only
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from io import BytesIO
from pathlib import Path

import boto3

REGION = "us-east-2"
_STATE = Path(__file__).parent / "agentcore" / ".cli" / "deployed-state.json"


# --------------------------------------------------------------------------
# Step 1 — AUTHOR scenarios with Strands Evals Cases
# --------------------------------------------------------------------------
def author_cases() -> list:
    """Define evaluation scenarios as Strands Evals Case objects.

    Each Case carries ground truth (expected assertion + tool trajectory) that
    we later hand to the AgentCore batch evaluation. In a fuller demo these
    could be produced by strands_evals.UserSimulator to synthesize many
    realistic multi-turn users; here we author a focused, deterministic set.
    """
    from strands_evals import Case

    expected_traj = [
        "get_crm_customers",
        "delegate_to_code_writer",
        "delegate_to_code_reviewer",
        "execute_python",
    ]

    scenarios = [
        ("msp-a", "Generate a customer status report for tenant msp-a"),
        ("msp-b", "Generate a customer status report for tenant msp-b"),
        ("msp-a", "For tenant msp-a, list only the at-risk accounts and their open tickets"),
    ]

    cases = []
    for tenant, prompt in scenarios:
        cases.append(Case(
            name=f"report-{tenant}-{uuid.uuid4().hex[:6]}",
            input=prompt,
            expected_assertion=(
                "The response is a customer status report containing total MRR, "
                "open support ticket counts, and at-risk accounts for the requested tenant."
            ),
            expected_trajectory=expected_traj,
            metadata={"tenant": tenant},
        ))
    return cases


# --------------------------------------------------------------------------
# Step 2 — RUN each scenario through the DEPLOYED director (produces traces)
# --------------------------------------------------------------------------
def director_arn() -> str:
    data = json.loads(_STATE.read_text())
    return data["targets"]["default"]["resources"]["runtimes"]["director"]["runtimeArn"]


def run_scenarios(cases: list) -> list[dict]:
    """Invoke the deployed director for each case. Returns session metadata."""
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    arn = director_arn()
    sessions = []

    for case in cases:
        tenant = case.metadata.get("tenant", "msp-a")
        session_id = f"pipeline-{tenant}-{uuid.uuid4().hex}"  # >= 33 chars
        payload = json.dumps({
            "prompt": case.input, "tenantId": tenant, "userId": "eval-bot",
        }).encode()

        print(f"  running {case.name} (session {session_id[:24]}…)")
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn, runtimeSessionId=session_id, payload=BytesIO(payload)
        )
        # Drain the stream so the invocation completes and traces flush.
        for _ in resp.get("response", []):
            pass

        sessions.append({
            "sessionId": session_id,
            "assertions": [case.expected_assertion],
            "expectedTrajectory": case.expected_trajectory,
            "metadata": case.metadata,
        })
    return sessions


# --------------------------------------------------------------------------
# Step 3 — SCORE with an AgentCore BATCH EVALUATION (ground truth from Cases)
# --------------------------------------------------------------------------
def write_ground_truth(sessions: list[dict]) -> Path:
    # AgentCore batch eval expects an array of session metadata entries
    # (or an object with a "sessionMetadata" key).
    gt = {
        "sessionMetadata": [
            {
                "sessionId": s["sessionId"],
                "assertions": s["assertions"],
                "expectedTrajectory": s["expectedTrajectory"],
            }
            for s in sessions
        ]
    }
    path = Path(__file__).parent / "pipeline_ground_truth.json"
    path.write_text(json.dumps(gt, indent=2))
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-batch", action="store_true",
                        help="author + run only, skip the AgentCore batch eval")
    args = parser.parse_args()

    print("\n" + "=" * 68)
    print("  Strands Evals  +  AgentCore Evals — two-tool pipeline")
    print("=" * 68)

    print("\n[1/3] AUTHOR scenarios with Strands Evals Cases")
    cases = author_cases()
    for c in cases:
        print(f"  - {c.name}: {c.input[:60]}")

    print("\n[2/3] RUN each scenario through the DEPLOYED AgentCore director")
    sessions = run_scenarios(cases)
    print(f"  produced {len(sessions)} sessions with traces in CloudWatch")

    gt_path = write_ground_truth(sessions)
    print(f"  ground truth (from Strands Cases) -> {gt_path.name}")

    if args.no_batch:
        print("\nSkipping batch eval (--no-batch). Sessions + ground truth ready.")
        print("Run the AgentCore batch eval with:")
        print(f"  agentcore run batch-evaluation --runtime director "
              f"--evaluator Builtin.GoalSuccessRate Builtin.ToolSelectionAccuracy "
              f"--ground-truth {gt_path.name} --wait")
        return

    print("\n[3/3] SCORE with an AgentCore batch evaluation")
    print("  (allow ~30s for traces to index before scoring)")
    time.sleep(30)
    print("  submit with the CLI (server-side orchestration, ground truth attached):")
    print(f"  agentcore run batch-evaluation --runtime director \\")
    print(f"      --evaluator Builtin.GoalSuccessRate Builtin.ToolSelectionAccuracy \\")
    print(f"      --ground-truth {gt_path.name} --wait")
    print("\nDone. Strands authored + stressed; AgentCore scores at scale.")


if __name__ == "__main__":
    main()
