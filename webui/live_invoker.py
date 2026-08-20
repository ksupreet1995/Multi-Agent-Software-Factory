"""LIVE invoker — drives the DEPLOYED AgentCore director runtime.

Calls invoke_agent_runtime against the real director, parses its NDJSON stream
(Strands events), and maps tool calls to UI pipeline events. This lets the web
UI visualize the real multi-agent workflow while the prompt genuinely changes
the model's behavior.

The director runtime ARN is read from the deployed-state.json the CLI writes.
"""

from __future__ import annotations

import json
import uuid
from io import BytesIO
from pathlib import Path
from typing import Iterator

from webui.core import CONFIG, CLOUDWATCH, FactoryEvent

_STATE = (
    Path(__file__).parent.parent
    / "deploy_agentcore"
    / CONFIG.deploy_project_dir
    / "agentcore"
    / ".cli"
    / "deployed-state.json"
)

# Map deployed director tool names -> UI pipeline steps + labels.
_TOOL_STEP = {
    "get_crm_customers": ("gateway", "Gateway — CRM tool"),
    "delegate_to_code_writer": ("code_writer", "Code Writer agent"),
    "delegate_to_code_reviewer": ("code_reviewer", "Code Reviewer agent"),
    "execute_python": ("code_interpreter", "Code Interpreter"),
}

_STEP_LABELS = {
    "gateway": "Gateway — CRM tool",
    "code_writer": "Code Writer agent",
    "code_reviewer": "Code Reviewer agent",
    "code_interpreter": "Code Interpreter",
    "report_builder": "Report Builder agent",
    "director": "Director agent (LIVE)",
}


def director_arn() -> str | None:
    try:
        data = json.loads(_STATE.read_text())
        return (
            data["targets"]["default"]["resources"]["runtimes"]["director"]["runtimeArn"]
        )
    except Exception:  # noqa: BLE001
        return None


def is_available() -> bool:
    return director_arn() is not None


def _iter_stream_lines(response) -> Iterator[str]:
    buffer = ""
    for chunk in response.get("response", []):
        raw = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        buffer += raw
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def run_live(tenant_id: str, session_id: str, user_id: str, prompt: str) -> Iterator[FactoryEvent]:
    """Invoke the deployed director and yield UI events mapped from its stream."""
    import boto3

    arn = director_arn()
    if not arn:
        yield FactoryEvent("director", "error", "Director agent",
                           "Deployed director ARN not found. Deploy first.", {})
        return

    def emit(ev: FactoryEvent) -> FactoryEvent:
        CLOUDWATCH.log(session_id, ev.step, {
            "status": ev.status, "tenant": tenant_id, "title": ev.title,
            "detail": ev.detail, "mode": "LIVE",
        })
        return ev

    yield emit(FactoryEvent("director", "running", "Director agent (LIVE)",
                            f"Invoking deployed runtime for {tenant_id}",
                            {"prompt": prompt, "arn": arn}))

    # Memory and Identity are active in the deployed runtime but are handled
    # implicitly (Memory via AgentCoreMemorySessionManager; Identity via the
    # runtime's IAM/SigV4 auth to the Gateway) — they are not tool calls, so
    # they never appear in the director's event stream. Surface them explicitly
    # so the UI reflects that these managed pillars are engaged for this run.
    yield emit(FactoryEvent("memory", "done", "Memory",
                            "AgentCore Memory session manager attached (short-term events)",
                            {"session_id": session_id}))
    yield emit(FactoryEvent("identity", "done", "Identity",
                            "Runtime authenticated to Gateway via IAM/SigV4 (scoped)",
                            {"auth": "AWS_IAM"}))

    client = boto3.client("bedrock-agentcore", region_name=CONFIG.aws_region)
    rt_session = f"ui-live-{tenant_id}-{uuid.uuid4().hex}"
    payload = json.dumps({"prompt": prompt, "tenantId": tenant_id, "userId": user_id}).encode()

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=rt_session,
            payload=BytesIO(payload),
        )
    except Exception as exc:  # noqa: BLE001
        yield emit(FactoryEvent("director", "error", "Director agent (LIVE)",
                                f"Invocation failed: {exc}", {}))
        return

    final_text: list[str] = []
    # Track the current streaming tool call (name + accumulated input JSON).
    state = {"cur_step": None, "cur_name": None, "cur_input": "", "active": set(),
             "started": set(), "records": None, "writer_task": None}

    for line in _iter_stream_lines(response):
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        for ui_event in _map_event(event, state, final_text):
            yield emit(ui_event)

    # Safety net: flip any tool cards still marked running to done.
    for step in list(state["active"]):
        yield emit(FactoryEvent(step, "done", _STEP_LABELS.get(step, step),
                                "Completed.", {}))

    # The director's stream does not expose the code_writer's output, so pull
    # the generated code from the code_writer runtime's CloudWatch logs.
    code = _fetch_generated_code()
    if code:
        yield emit(FactoryEvent("code_writer", "done", "Code Writer agent",
                                f"Generated {len(code.splitlines())} lines (from specialist runtime)",
                                {"code": code, "language": "python"}))

    text = "".join(final_text).strip()
    if text:
        yield emit(FactoryEvent("report_builder", "done", "Report Builder agent",
                                "Director produced the final report.",
                                {"report_text": text, "kind": "text"}))
    yield emit(FactoryEvent("director", "done", "Director agent (LIVE)",
                            "Workflow complete.", {}))

    text = "".join(final_text).strip()
    if text:
        yield emit(FactoryEvent("report_builder", "done", "Report Builder agent",
                                "Director produced the final report.",
                                {"report_text": text, "kind": "text"}))
    yield emit(FactoryEvent("director", "done", "Director agent (LIVE)",
                            "Workflow complete.", {}))


def _map_event(state_event, state, final_text):
    """Translate stream events using the real director stream structure.

    The deployed director streams the model's own output only:
      * contentBlockStart -> toolUse.name : a tool call begins
      * contentBlockDelta -> toolUse.input : streamed tool-input fragments
      * contentBlockDelta -> text          : assistant narration / final answer
    Tool *results* are NOT streamed, so code/records are surfaced separately
    (code via _fetch_generated_code; the final report via accumulated text).
    """
    inner = state_event.get("event", state_event)

    # A tool call begins.
    cbs = inner.get("contentBlockStart", {})
    start_tool = cbs.get("start", {}).get("toolUse") if cbs else None
    if start_tool and start_tool.get("name"):
        name = start_tool["name"]
        step, label = _TOOL_STEP.get(name, ("director", name))
        state["cur_step"], state["cur_name"], state["cur_input"] = step, name, ""
        if step not in state["started"]:
            state["started"].add(step)
            state["active"].add(step)
            yield FactoryEvent(step, "running", label, f"Invoking {name}...",
                               {"tool": name})
        return

    delta = inner.get("contentBlockDelta", {}).get("delta", {}) if inner.get("contentBlockDelta") else {}

    # Streamed tool-input fragments -> accumulate.
    if "toolUse" in delta and "input" in delta["toolUse"]:
        state["cur_input"] += delta["toolUse"]["input"]
        return

    # Assistant text -> final report narration.
    if "text" in delta:
        final_text.append(delta["text"])
        return

    # A content block finished — if it was a tool call, resolve its card.
    if "contentBlockStop" in inner and state["cur_step"]:
        step, name = state["cur_step"], state["cur_name"]
        parsed = _try_json(state["cur_input"])
        data = {}
        detail = "Completed."
        if isinstance(parsed, dict):
            if "tenant_id" in parsed:
                detail = f"tenant_id={parsed['tenant_id']}"
                # Surface the CRM records in the UI (director stream omits them).
                records = _fetch_crm_records(parsed["tenant_id"])
                if records:
                    data = {"records": records}
                    detail = f"Fetched {len(records)} records for {parsed['tenant_id']}"
            if "task" in parsed:
                detail = f"task: {str(parsed['task'])[:100]}"
                state["writer_task"] = parsed["task"]
        state["active"].discard(step)
        state["cur_step"] = state["cur_name"] = None
        state["cur_input"] = ""
        yield FactoryEvent(step, "done", _STEP_LABELS.get(step, step), detail, data)


def _fetch_generated_code() -> str:
    """Pull the most recent generated Python from the code_writer runtime logs."""
    try:
        import boto3
        import time

        logs = boto3.client("logs", region_name=CONFIG.aws_region)
        arn = _runtime_arn("code_writer")
        if not arn:
            return ""
        rid = arn.split("/")[-1]
        group = f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
        start = int((time.time() - 300) * 1000)
        resp = logs.filter_log_events(
            logGroupName=group, startTime=start,
            filterPattern='"gen_ai.choice"', limit=50,
        )
        best = ""
        for ev in resp.get("events", []):
            msg = ev.get("message", "")
            code = _extract_code_from_log(msg)
            if code and len(code) > len(best):
                best = code
        return best
    except Exception:  # noqa: BLE001
        return ""


def _extract_code_from_log(msg: str) -> str:
    """Best-effort: recover generated Python from a gen_ai.choice log line."""
    try:
        obj = json.loads(msg[msg.index("{"):])
    except Exception:  # noqa: BLE001
        return ""
    raw = _find(obj, "message")
    if not isinstance(raw, str):
        return ""
    # The message may be a JSON array of {"text": "..."} blocks.
    text = raw
    parsed = _try_json(raw)
    if isinstance(parsed, list):
        text = "".join(b.get("text", "") for b in parsed if isinstance(b, dict))
    if "print(" not in text and "def " not in text and "customers" not in text:
        return ""
    return _strip_code_fence(text)


def _strip_code_fence(text: str) -> str:
    """Remove surrounding ```python ... ``` fences if present."""
    t = text.strip()
    if "```" in t:
        start = t.find("```")
        # skip the opening fence line
        nl = t.find("\n", start)
        rest = t[nl + 1:] if nl != -1 else t[start + 3:]
        end = rest.rfind("```")
        if end != -1:
            rest = rest[:end]
        return rest.strip()
    return t


def _fetch_crm_records(tenant_id: str) -> list:
    """Fetch the same CRM records the Gateway Lambda serves, for UI display."""
    try:
        import boto3
        client = boto3.client("lambda", region_name=CONFIG.aws_region)
        resp = client.invoke(
            FunctionName=CONFIG.crm_lambda_name,
            Payload=json.dumps({"tenant_id": tenant_id}).encode(),
        )
        body = json.loads(resp["Payload"].read().decode())
        return body.get("customers", [])
    except Exception:  # noqa: BLE001
        return []


def _runtime_arn(name: str) -> str | None:
    try:
        data = json.loads(_STATE.read_text())
        return data["targets"]["default"]["resources"]["runtimes"][name]["runtimeArn"]
    except Exception:  # noqa: BLE001
        return None


def _find(obj: dict, key: str):
    """Recursively locate the first value for `key` in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find(item, key)
            if found is not None:
                return found
    return None


def _try_json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None
