"""Director-side tools that invoke specialist agents on their own runtimes.

This is the real multi-agent "software factory": the director does not generate
or review code itself — it delegates to independently deployed AgentCore
runtimes (code_writer, code_reviewer) over the network via invoke_agent_runtime.

Each specialist runtime ARN is provided via env vars injected by the CDK stack
(CODE_WRITER_RUNTIME_ARN, CODE_REVIEWER_RUNTIME_ARN).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from io import BytesIO

import boto3
from strands import tool

from factory_tools import _strip_code_fences

logger = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "us-east-2")
CODE_WRITER_ARN = os.getenv("CODE_WRITER_RUNTIME_ARN", "")
CODE_REVIEWER_ARN = os.getenv("CODE_REVIEWER_RUNTIME_ARN", "")

_client = boto3.client("bedrock-agentcore", region_name=REGION)


def _invoke_runtime(runtime_arn: str, prompt: str) -> str:
    """Invoke a specialist AgentCore runtime and return its full text response."""
    if not runtime_arn:
        return "ERROR: specialist runtime ARN not configured."

    # runtimeSessionId must be >= 33 chars.
    session_id = f"director-delegation-{uuid.uuid4().hex}"
    payload = json.dumps({"prompt": prompt}).encode("utf-8")

    response = _client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=BytesIO(payload),
    )

    # The specialist streams NDJSON events; accumulate text deltas.
    text_parts: list[str] = []
    for chunk in response.get("response", []):
        raw = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[len("data:"):].strip()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            delta = (
                event.get("event", {})
                .get("contentBlockDelta", {})
                .get("delta", {})
                .get("text")
            )
            if delta:
                text_parts.append(delta)
    return "".join(text_parts).strip()


@tool
def delegate_to_code_writer(task: str, crm_data_json: str) -> str:
    """Delegate code generation to the Code Writer specialist runtime.

    Args:
        task: What the script should do (e.g. build a customer status report).
        crm_data_json: The CRM data as a JSON string for the script to use.

    Returns:
        A self-contained Python script produced by the specialist.
    """
    logger.info("Delegating to code_writer runtime")
    prompt = (
        f"{task}\n\nUse exactly this data (already fetched from the CRM):\n"
        f"customers = {crm_data_json}"
    )
    code = _invoke_runtime(CODE_WRITER_ARN, prompt)
    # The writer occasionally wraps its script in markdown ```python ... ```
    # fences despite instructions not to. A bare ``` line is itself a Python
    # syntax error, so if we forward the fenced text to the reviewer it gets
    # (correctly) REJECTED and the whole multi-agent loop fails. Strip fences
    # here, at the boundary, so BOTH the reviewer and the sandbox see clean
    # Python — not just execute_python (which strips them too, as a backstop).
    return _strip_code_fences(code)


@tool
def delegate_to_code_reviewer(code: str) -> str:
    """Delegate a safety + correctness review to the Code Reviewer specialist runtime.

    Args:
        code: The Python script to review before execution.

    Returns:
        A verdict starting with APPROVED or REJECTED plus a one-line reason.
    """
    logger.info("Delegating to code_reviewer runtime")
    return _invoke_runtime(CODE_REVIEWER_ARN, f"Review this code:\n\n{code}")
