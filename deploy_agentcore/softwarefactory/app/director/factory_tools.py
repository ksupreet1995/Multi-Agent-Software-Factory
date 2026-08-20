"""Software Factory tools exposed to the director agent (deployed to AgentCore).

These are self-contained (no dependency on the local demo package) so they ship
inside the Runtime CodeZip. They exercise:

  * an external CRM API through a real AgentCore Gateway (MCP)  -> Gateway story
  * AgentCore Code Interpreter                                  -> sandbox story

If CRM_GATEWAY_URL is set, get_crm_customers routes through the deployed
AgentCore Gateway (SigV4-signed MCP JSON-RPC to the Lambda target). Otherwise it
falls back to local data so the tool still works in local/dev runs.
"""

from __future__ import annotations

import json
import logging
import os

from strands import tool

logger = logging.getLogger(__name__)

CRM_GATEWAY_URL = os.getenv("CRM_GATEWAY_URL", "")
REGION = os.getenv("AWS_REGION", "us-east-2")
CRM_TOOL_NAME = "crmTarget___get_crm_customers"

# Local fallback data (used only when no Gateway URL is configured).
_CRM_DATA = {
    "msp-a": [
        {"name": "Northwind Traders", "mrr": 4200, "status": "active", "tickets_open": 3},
        {"name": "Contoso Ltd", "mrr": 8800, "status": "active", "tickets_open": 1},
        {"name": "Fabrikam Inc", "mrr": 1500, "status": "at_risk", "tickets_open": 7},
        {"name": "Adventure Works", "mrr": 6300, "status": "active", "tickets_open": 0},
    ],
    "msp-b": [
        {"name": "Tailspin Toys", "mrr": 2100, "status": "active", "tickets_open": 2},
        {"name": "Wingtip Toys", "mrr": 9600, "status": "active", "tickets_open": 4},
        {"name": "Proseware Systems", "mrr": 3300, "status": "at_risk", "tickets_open": 9},
    ],
}


def _fetch_via_gateway(tenant_id: str) -> list[dict]:
    """Call the CRM tool through the AgentCore Gateway using a signed MCP request."""
    import boto3
    import botocore.auth
    import botocore.awsrequest
    import urllib.request

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": CRM_TOOL_NAME, "arguments": {"tenant_id": tenant_id}},
        }
    )

    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    aws_request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=CRM_GATEWAY_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    botocore.auth.SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(aws_request)

    req = urllib.request.Request(
        CRM_GATEWAY_URL, data=body.encode("utf-8"), headers=dict(aws_request.headers)
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")

    # Gateway may respond as JSON or as an SSE stream ("data: {...}").
    payload = _parse_mcp_response(raw)
    content = payload.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            data = json.loads(item["text"])
            return data.get("customers", [])
    return []


def _parse_mcp_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    # SSE framing: find the last "data:" line.
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return {}


def _strip_code_fences(code: str) -> str:
    """Remove markdown ```python ... ``` fences that specialists sometimes add.

    The code_writer occasionally wraps its output in a fenced block; passing that
    verbatim to the sandbox causes a syntax error on the ``` line. Strip it so
    the executed code is pure Python.
    """
    text = (code or "").strip()
    if "```" not in text:
        return text
    start = text.find("```")
    nl = text.find("\n", start)
    body = text[nl + 1:] if nl != -1 else text[start + 3:]
    end = body.rfind("```")
    if end != -1:
        body = body[:end]
    return body.strip()


@tool
def get_crm_customers(tenant_id: str) -> str:
    """Fetch the CRM customer records for a tenant.

    Routes through the AgentCore Gateway when configured, otherwise uses local
    data. Returns a JSON string of records (name, mrr, status, tickets_open).

    Args:
        tenant_id: The MSP tenant id, e.g. 'msp-a' or 'msp-b'.
    """
    if CRM_GATEWAY_URL:
        try:
            records = _fetch_via_gateway(tenant_id)
            logger.info("CRM via Gateway tenant=%s records=%d", tenant_id, len(records))
            return json.dumps(records)
        except Exception as exc:  # noqa: BLE001 - fall back so the demo is resilient
            logger.warning("Gateway CRM call failed (%s); using local fallback", exc)

    records = sorted(
        _CRM_DATA.get(tenant_id, []), key=lambda r: r["mrr"], reverse=True
    )
    logger.info("CRM local tenant=%s records=%d", tenant_id, len(records))
    return json.dumps(records)


@tool
def execute_python(code: str) -> str:
    """Execute Python code in the AgentCore Code Interpreter sandbox.

    Use this to compute aggregates or build a report from CRM data. The code
    must print its results to stdout.

    Args:
        code: A self-contained Python script that prints its output.

    Returns:
        The stdout produced by the code, or an error message.
    """
    try:
        from bedrock_agentcore.tools.code_interpreter_client import code_session
    except ImportError:  # pragma: no cover
        return "Code Interpreter SDK not available in this runtime."

    code = _strip_code_fences(code)

    try:
        with code_session(REGION) as client:
            response = client.invoke("executeCode", {"language": "python", "code": code})
        results = []
        for event in response.get("stream", []):
            for item in event.get("result", {}).get("content", []):
                if item.get("type") == "text":
                    results.append(item["text"])
        return "\n".join(results) if results else "(no output)"
    except Exception as exc:  # noqa: BLE001 - surface sandbox errors to the model
        logger.exception("Code Interpreter failed")
        return f"Code Interpreter error: {type(exc).__name__}: {exc}"
