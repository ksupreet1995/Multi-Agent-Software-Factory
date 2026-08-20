# Multi-Agent Software Factory — on Amazon Bedrock AgentCore

A runnable demo that showcases all seven Amazon Bedrock AgentCore pillars in one
coherent story: an engineer directs an AI **director agent** to pull data from a
CRM, generate and review code, execute it in a sandbox, and return a
tenant-scoped report — while every step is secured, isolated, and observed.

Everything runs against **real, deployed AgentCore resources** — the director
and specialists are live runtimes, the CRM is a real Lambda behind an MCP
gateway, and every step emits traces to CloudWatch.

---

## Architecture

This is a **supervisor (orchestrator) multi-agent pattern**. The **Director** is
the supervisor — it holds the goal, calls the CRM tool for data, delegates
writing and reviewing to specialist agents that each run on their own runtime,
then executes the approved code in a sandbox. Specialists never talk to each
other; everything routes through the supervisor, which keeps control, retries,
and tenant isolation in one place.

The client invokes the **Director Runtime directly** (`invoke_agent_runtime`,
IAM/SigV4). The Gateway is *not* the front door — it's one of the Director's
tools, used **outbound** to reach the CRM.

```
Engineer / Web UI
      │  invoke_agent_runtime (IAM / SigV4)
      ▼
┌─────────────────────────────────────────────────────────┐
│  Director Runtime  (supervisor / orchestrator)           │
│    · Identity  → JIT scoped, short-lived tokens          │
│    · Memory    → per-session state                       │
│                                                          │
│  runs these in sequence (tools + delegations interleaved):│
│                                                          │
│   1. CRM Tool          → outbound: Director → Gateway →  │
│                          Lambda (per-tenant CRM data)    │
│   2. Code Writer       → delegate: generates Python      │  each specialist
│   3. Code Reviewer     → delegate: approves / rejects    │  is its own
│                          (loops back to 2, max 2 retries)│  AgentCore Runtime
│   4. Code Interpreter  → tool: runs approved code in a   │
│                          secure sandbox                  │
└─────────────────────────────────────────────────────────┘
      │
      ▼  final report  +  full trace
  Observability → CloudWatch (traces · P50/P95/P99 · tokens, every step)
  Evaluations   → online (continuous) + offline (on-demand / batch),
                  LLM-as-a-judge AND code-based (deterministic) evaluators
```

### The seven pillars, mapped

| Pillar | Role in the factory |
|---|---|
| **Runtime** | Hosts the director + each specialist as independently-scaling agents |
| **Gateway** | Exposes the CRM (a Lambda) as an MCP tool; per-tenant routing + auth |
| **Identity** | Issues JIT, scoped, short-lived tokens per session/tenant |
| **Memory** | Persists session state across a multi-step workflow |
| **Code Interpreter** | Runs the generated Python in an isolated sandbox |
| **Observability** | CloudWatch traces + latency percentiles + token usage per step |
| **Evaluations** | Scores quality online + offline; managed judge and custom evaluators |

---

## Prerequisites

```bash
npm install -g @aws/agentcore     # the CDK-based AgentCore CLI
aws sts get-caller-identity       # confirm AWS creds
pip install -e .                  # Python deps for the web UI + demo scripts
```

Copy `.env.example` to `.env` and fill in your deployment's resource names
(the repo ships generic defaults; `.env` is git-ignored):

```bash
cp .env.example .env
# then edit AWS_REGION, CRM_LAMBDA_NAME, UI_LOG_GROUP, etc.
```

## Deploy to AgentCore

The deployment lives under `deploy_agentcore/softwarefactory/` and is managed by
the **AgentCore CLI (`@aws/agentcore`)**, a CDK-based tool. The project was
scaffolded with:

```bash
agentcore create --project-name softwarefactory --name director \
    --language Python --framework Strands --model-provider Bedrock \
    --memory shortTerm --build CodeZip --protocol HTTP
```

Then, from `deploy_agentcore/softwarefactory/`:

```bash
# one-time: install CDK deps
npm install --prefix agentcore/cdk

# deploy runtimes (director + specialists) + memory + gateway + evaluators + IAM
agentcore deploy --yes

# invoke the live director (session id must be >= 33 chars).
# --runtime director is required because specialists are separate runtimes.
# The director delegates to the code_writer and code_reviewer runtimes,
# then executes the approved script in Code Interpreter.
agentcore invoke --runtime director \
    --prompt "Generate a customer status report for tenant msp-a" \
    --session-id "msp-a-eng1-live-000000000000000001"

# call the CRM straight through the Gateway (MCP)
agentcore invoke --gateway crmGateway \
    --tool "crmTarget___get_crm_customers" --input '{"tenant_id":"msp-b"}'

# tail logs / view status
agentcore status
agentcore logs --runtime director
```

Everything is real: the director + `code_writer` + `code_reviewer` each run as
their own AgentCore Runtime; the CRM is a real Lambda (`lambda_crm/handler.py`)
fronted by an MCP Gateway; the director's `get_crm_customers` tool SigV4-signs
an MCP call to that Gateway; the approved script runs in the Code Interpreter
sandbox; and every step emits OTEL traces to CloudWatch.

### Standalone capability demos

Each of these exercises one pillar against the live deployment (run from
`deploy_agentcore/softwarefactory/`):

```bash
python identity_demo.py          # JIT scoped workload tokens per tenant
python policy_demo.py            # Cedar per-user tool access (analyst vs admin)
python interceptor_demo.py       # Gateway request interceptor (finest-grained)
python longterm_memory_demo.py   # long-term semantic/summary memory
python strands_agentcore_pipeline.py   # Strands Evals -> AgentCore batch eval
```

## Web UI

A single-page UI that visualizes the deployed workflow as it runs — agents and
tool invocations, the generated Python, the per-tenant CRM data, and the
resulting PowerPoint (with a download button). Every step is mirrored to
**CloudWatch Logs** in real time, so you can flip to the AWS console and show
the same invocations landing there.

```bash
python -m webui.server
# open the URL it prints (default http://127.0.0.1:8090)
```

What you'll see:
- **Left**: pick a tenant, enter a prompt, hit Run. A pipeline mini-map lights
  up each step as the deployed director + specialist runtimes execute.
- **Middle**: a streaming feed of every agent/tool invocation with status.
- **Right**: tabs for the generated Python, the CRM data table, the report
  slide + `.pptx` download, an **Architecture** view, and **Evals**.
- **Evals tab**:
  1. the deployed **online** eval config status (ACTIVE, continuously scoring
     live traffic),
  2. a button to run an **on-demand (offline)** evaluation — real scoring of the
     director's recent traces (both LLM-as-a-judge and code-based evaluators),
     rendered as per-evaluator scores + per-session explanations, and
  3. **Strands Evals → AgentCore Evals pipeline** — authors scenarios with
     Strands Evals, runs them through the deployed director, and scores them
     with an AgentCore batch evaluation (Author → Run → Score).

  See `deploy_agentcore/softwarefactory/EVALS.md` for the full evals story.

CloudWatch: the UI logs to the group set by `UI_LOG_GROUP`
(one stream per session). The top bar shows whether CloudWatch is connected.

> Set `WEBUI_PORT` to change the port and `AWS_REGION` to control which region
> CloudWatch logs land in (both can also live in `.env`).

---

## Project layout

```
webui/                    # LIVE web UI (invokes the deployed director)
  server.py               # stdlib HTTP server + SSE streaming
  core.py                 # config, CloudWatch logging, events, tenants
  live_invoker.py         # invokes the deployed AgentCore director runtime
  evals.py                # on-demand AgentCore evaluations
  pipeline.py             # Strands Evals -> AgentCore batch-eval pipeline
  index.html / app.js / styles.css

deploy_agentcore/softwarefactory/   # the AgentCore deployment (CLI / CDK)
  app/
    director/             # supervisor runtime
      main.py             # entrypoint + system prompt + tenant binding
      factory_tools.py    # CRM-via-Gateway + Code Interpreter tools
      specialists.py      # delegation to code_writer / code_reviewer runtimes
    code_writer/          # specialist runtime — generates Python
    code_reviewer/        # specialist runtime — approves / rejects
  lambda_crm/handler.py   # CRM backend Lambda (Gateway target)
  lambda_interceptor/     # Gateway request interceptor (finest-grained access)
  eval_code/              # code-based (deterministic) evaluator Lambda
  agentcore/
    agentcore.json        # project config (runtimes, memory, gateways, evals)
    cdk/lib/cdk-stack.ts  # CDK stack (+ Code Interpreter/Gateway IAM grants)
  EVALS.md POLICY.md INTERCEPTOR.md   # capability deep-dives
  identity_demo.py policy_demo.py interceptor_demo.py
  longterm_memory_demo.py strands_agentcore_pipeline.py
```

## Notes & caveats

- The AgentCore starter-toolkit CLI is legacy; this project uses the newer
  CDK-based `@aws/agentcore` CLI. Verify the CLI available in your
  account/region before deploying.
- Long-term Memory extraction is asynchronous (10–30s). The deployed director
  uses short-term Events (synchronous) for session state.
- Deployment identifiers (account, resource names) are generic placeholders in
  the committed source. Set your real values in `.env` — it's git-ignored.
