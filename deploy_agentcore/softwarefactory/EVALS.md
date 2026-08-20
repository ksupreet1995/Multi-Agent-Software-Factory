# AgentCore Evaluations — Online + Offline (demo guide)

AgentCore offers three evaluation types. This project demonstrates all of them
against the deployed `director` runtime. Run everything from
`deploy_agentcore/softwarefactory/` with `$env:AWS_REGION="us-east-2"`.

| Type | When | How | Command |
|---|---|---|---|
| **Online** | continuous, live traffic | service samples sessions + scores automatically | config `director_online_evals` (deployed, ACTIVE) |
| **On-demand** | offline, targeted | you pick traces/sessions, service scores them | `agentcore run eval` |
| **Batch** | offline, many sessions | async job over a CloudWatch time window | `agentcore run batch-evaluation` |

Two evaluator *styles* are demonstrated:

- **LLM-as-a-judge** — built-ins `Builtin.GoalSuccessRate`,
  `Builtin.ToolSelectionAccuracy`, `Builtin.Helpfulness`, plus a custom one,
  `report_quality` (our own Bedrock judge model + rubric).
- **Code-based (deterministic, no LLM)** — a custom `report_schema_check`
  evaluator (a Lambda) that mechanically verifies the report contains the
  required fields. Same input → same score, every run; zero LLM inference cost.
  See "Code-based evaluator" below.

---

## 1. Offline — on-demand evaluation (fast, targeted)

Score the director's recent traces. Great for build-time / regression checks.

```powershell
# score the last day of director sessions on two built-in evaluators
agentcore run eval --runtime director `
  --evaluator Builtin.GoalSuccessRate Builtin.Helpfulness `
  --days 1

# with GROUND TRUTH against a single session (assertion + expected tool trajectory)
agentcore run eval --runtime director --session-id "<SESSION_ID>" `
  --evaluator Builtin.GoalSuccessRate Builtin.ToolSelectionAccuracy `
  --expected-trajectory "get_crm_customers,delegate_to_code_writer,delegate_to_code_reviewer,execute_python" `
  --assertion "The report includes total MRR, open support tickets, and at-risk accounts"
```

Proven result: **GoalSuccessRate 1.0** and **Helpfulness 1.0** across 13 sessions,
each with a written LLM explanation (it even flagged the early sessions where
Code Interpreter lacked permissions and the agent compensated).

## 2. Offline — batch evaluation (many sessions, async job)

Baseline / pre-post comparison across a time window. Server-side orchestration.

```powershell
agentcore run batch-evaluation --runtime director `
  --evaluator Builtin.GoalSuccessRate Builtin.ToolSelectionAccuracy Builtin.Helpfulness `
  --lookback-days 1 --name director_baseline --wait
```

### The judge model

Built-in evaluators (GoalSuccessRate, ToolSelectionAccuracy, Helpfulness) are
LLM-as-a-judge on an AWS-managed model. For a **custom** evaluator you specify
the judge yourself via `evaluatorConfig.llmAsAJudge.modelConfig.
bedrockEvaluatorModelConfig.modelId` (plus inference settings) — our
`report_quality` evaluator uses `us.anthropic.claude-sonnet-4-...`. You can also
create a **code-based** evaluator (a Lambda) for fully deterministic, non-LLM
scoring. Note: ToolSelectionAccuracy compares the actual tool calls against the
supplied `expected_trajectory` — a structural check, which is why it scores 1.0
when the director follows the expected tool order.

Results (aggregate per-evaluator averages + per-session detail) land in
CloudWatch. View past runs with `agentcore batch-evaluations <id>` or `agentcore view`.

## Code-based evaluator — `report_schema_check` (deterministic, no LLM)

`report_quality` (LLM judge) and `report_schema_check` (code-based) sit side by
side so the customer can see both styles on the same runs:

| | `report_quality` | `report_schema_check` |
|---|---|---|
| Style | LLM-as-a-judge | Code-based (Lambda) |
| Judge | Bedrock model you pick | your Python — no model |
| Determinism | non-deterministic (LLM) | **deterministic** (same in → same out) |
| Cost | per-call LLM inference | **~0** (tiny stdlib Lambda) |
| Answers | "is this report *good*?" | "does it *contain* the required fields?" |

The evaluator is `eval_code/report_schema_check.py` — standard-library only, so
the deploy bundle has no dependencies to build. It extracts the director's
output from the session spans and checks for three required fields (Total MRR,
open support tickets, at-risk accounts). Score = fields present / 3; label =
`Pass` only when all three are present.

Registered in `agentcore.json` as a **managed** code-based evaluator
(`config.codeBased.managed`), so `agentcore deploy` builds and deploys the
Lambda for you. It's wired into the online eval config too, so every sampled
session gets both the LLM score and the deterministic schema score.

```powershell
# score recent director sessions with the code-based evaluator (offline)
agentcore run eval --runtime director `
  --evaluator report_schema_check --days 1

# side-by-side: LLM judge + deterministic schema check on the same sessions
agentcore run eval --runtime director `
  --evaluator report_quality report_schema_check --days 1
```

## 3. Online — continuous evaluation (production monitoring)

Already deployed and **ACTIVE**: `director_online_evals` samples 100% of director
sessions and scores them with `Builtin.GoalSuccessRate`,
`Builtin.ToolSelectionAccuracy`, `Builtin.Helpfulness`, and `report_quality`.

```powershell
# generate live traffic, which the online config scores automatically
agentcore invoke --runtime director `
  --prompt "Generate a customer status report for tenant msp-a" `
  --session-id "msp-a-eng1-eval-000000000000000001"

# inspect / manage the online config
agentcore status
agentcore pause    # pause an online eval config
agentcore resume   # resume it
```

Scores accumulate in CloudWatch dashboards over time (trend lines, low-score
investigation, full interaction drill-down).

---

## Talk track

- **Offline** = build-time confidence + regression gate. Curate sessions or a
  dataset, assert expected tool trajectory + response, block a deploy if scores
  regress. Answers "did my prompt/model change make it worse?"
- **Online** = production assurance. Sample live traffic, score continuously,
  alert on drift — the observability story extended from "what happened" to
  "was it good." Answers "is quality holding up in production right now?"
- Both use the same evaluators, so a metric you gate on offline is the same one
  you monitor online.

## Resources deployed for evals

- Custom evaluator (LLM-as-judge): `report_quality`
- Custom evaluator (code-based, deterministic): `report_schema_check`
- Online eval config: `director_online_evals` (ACTIVE, 100% sampling)

---

## Strands Evals + AgentCore Evals — the two-tool pipeline

They compose because both operate on the same substrate: the OpenTelemetry
traces your Strands agents emit. Strands Evals is the framework-level, dev-time
harness (user simulation, test-case authoring, chaos/fault injection); AgentCore
Evals is the managed service (online + on-demand + batch, CloudWatch-integrated).

`strands_agentcore_pipeline.py` demonstrates the full loop:

1. **AUTHOR** — define scenarios + ground truth as Strands Evals `Case` objects
   (each carries `input`, `expected_assertion`, `expected_trajectory`).
2. **RUN** — invoke the deployed AgentCore director for each scenario, producing
   real sessions/traces in CloudWatch.
3. **SCORE** — submit an AgentCore **batch evaluation** over those sessions,
   passing the Strands-authored ground truth.

```powershell
# steps 1-2: author with Strands Evals, run through the deployed director
python strands_agentcore_pipeline.py --no-batch

# step 3: score with AgentCore batch eval (ground truth from the Strands Cases)
agentcore run batch-evaluation --runtime director `
  --evaluator Builtin.GoalSuccessRate Builtin.ToolSelectionAccuracy `
  --ground-truth pipeline_ground_truth.json --name strands_authored_batch --wait
```

Proven result: **GoalSuccessRate 1.00** (3 sessions) and
**ToolSelectionAccuracy 1.00** (15 tool calls) on Strands-authored scenarios.

Division of labor: **Strands Evals generates + stresses; AgentCore Evals scores
+ monitors at scale.** Same evaluator definitions can be used in both loops.

### Both tools scoring the same run (web UI pipeline)

The web UI's Evals tab runs a 4-stage version that shows **both** evaluation
systems scoring the identical runs:

1. **Author** — Strands Evals Cases (scenarios + ground truth)
2. **Run** — deployed AgentCore director (real CloudWatch sessions)
3. **Score (AgentCore)** — managed batch eval, built-in evaluators
4. **Score (Strands)** — local SDK `OutputEvaluator` rubrics on the same outputs

The two judges are independent and typically produce *different* scores on the
same run — which is the point: a managed service judge and a framework-level
judge cross-checking the same behavior, not rubber-stamping each other.
