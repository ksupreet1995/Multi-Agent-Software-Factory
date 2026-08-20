"""AgentCore Gateway REQUEST interceptor — finest-grained access control.

This is the third tier of access control (after JWT authorizer + Cedar Policy):
it inspects the actual TOOL ARGUMENTS at request time and can allow, transform,
or short-circuit (deny) the call before it ever reaches the target.

Business rules enforced here (argument-level, not identity-level):

  1. export_customer_pii must include a non-empty `justification` argument
     (audit/BYOD requirement) — otherwise the call is denied.
  2. get_crm_customers `limit` is capped at MAX_RECORDS — an over-limit request
     is transformed down to the cap (data-minimization), not rejected.

Mirrors the AWS "allow if refund < $100" interceptor pattern.

Contract (MCP target REQUEST interceptor):
  input : event["mcp"]["gatewayRequest"]["body"] = JSON-RPC tools/call
  output: {"interceptorOutputVersion":"1.0","mcp":{...}}
          - transformedGatewayRequest  -> forward (optionally modified) call
          - transformedGatewayResponse -> short-circuit with this response (deny)
"""

from __future__ import annotations

MAX_RECORDS = 3


def _deny(req_id, message: str) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32003,
                        "message": f"Blocked by Gateway interceptor: {message}",
                    },
                },
            }
        },
    }


def _forward(body: dict) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def handler(event, context):
    mcp = (event or {}).get("mcp", {})
    body = mcp.get("gatewayRequest", {}).get("body", {}) or {}
    req_id = body.get("id")
    method = body.get("method", "")

    # Only inspect tool calls; forward everything else (tools/list, etc.) as-is.
    if method != "tools/call":
        return _forward(body)

    params = body.get("params", {}) or {}
    tool = params.get("name", "")
    args = params.get("arguments", {}) or {}

    # Rule 1: PII export requires a justification argument.
    if tool.endswith("export_customer_pii"):
        justification = str(args.get("justification", "")).strip()
        if not justification:
            return _deny(
                req_id,
                "export_customer_pii requires a non-empty 'justification' argument "
                "(audit requirement).",
            )

    # Rule 2: cap record count for the customers tool (data minimization).
    if tool.endswith("get_crm_customers"):
        limit = args.get("limit")
        if isinstance(limit, int) and limit > MAX_RECORDS:
            new_args = dict(args)
            new_args["limit"] = MAX_RECORDS
            new_params = dict(params)
            new_params["arguments"] = new_args
            new_body = dict(body)
            new_body["params"] = new_params
            return _forward(new_body)

    # Default: forward unchanged.
    return _forward(body)
