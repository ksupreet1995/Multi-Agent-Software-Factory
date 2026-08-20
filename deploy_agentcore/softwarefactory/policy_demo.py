"""Fine-grained per-user tool access demo — AgentCore Policy (Cedar).

Same MCP gateway, same tools, two users. A Cedar policy at the Gateway permits
the sensitive `export_customer_pii` tool ONLY for callers whose OAuth scope
contains 'admin'. Enforcement happens at the Gateway, outside the agent code.

  analyst (scope: invoke)        -> get_crm_customers OK, export_customer_pii DENIED
  admin   (scope: invoke admin)  -> both tools OK

Usage:
    python policy_demo.py
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request

REGION = os.getenv("AWS_REGION", "us-east-2")
# Deployment-specific values — set these to your own deployment via env vars.
# (The Cognito domain and Gateway URL are unique per deployment.)
DOMAIN = os.getenv("POLICY_COGNITO_DOMAIN", "crm-policy-demo")
TOKEN_URL = f"https://{DOMAIN}.auth.{REGION}.amazoncognito.com/oauth2/token"
GATEWAY_URL = os.getenv(
    "POLICY_GATEWAY_URL",
    f"https://crm-policy-gw.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp",
)

CLIENTS = {
    "analyst": {
        "id": "795uf2nvusab43dtglhuur5i19",
        "secret": "dct3v8i5d3gfable3ldn805pdpqp815o73e4geftb18op8cu544",
        "scope": "crm-api/invoke",
    },
    "admin": {
        "id": "1dhk3caulnb6altohstk2mk0u7",
        "secret": "71qvptogg6hum5hklgqncppg7h6pmb15oocvbdhclp0df9jdr0r",
        "scope": "crm-api/invoke crm-api/admin",
    },
}


def get_token(client: dict) -> str:
    basic = base64.b64encode(f"{client['id']}:{client['secret']}".encode()).decode()
    body = f"grant_type=client_credentials&scope={client['scope'].replace(' ', '+')}".encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def call_tool(token: str, tool_name: str, tenant_id: str) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"tenant_id": tenant_id}},
    }).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:200]}
    return _parse(raw)


def list_tools(token: str) -> list[str]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = _parse(r.read().decode())
    except urllib.error.HTTPError as e:
        return [f"(error {e.code})"]
    return [t.get("name") for t in data.get("result", {}).get("tools", [])]


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {}


def _verdict(resp: dict) -> str:
    if resp.get("http_error"):
        return f"DENIED (HTTP {resp['http_error']})"
    if resp.get("result", {}).get("isError"):
        return "DENIED (policy)"
    if "error" in resp:
        return f"DENIED ({str(resp['error'])[:60]})"
    if resp.get("result"):
        return "ALLOWED"
    return f"? {str(resp)[:80]}"


def main() -> None:
    print("\n" + "=" * 68)
    print("  AgentCore Policy (Cedar) — per-user tool access at the Gateway")
    print("=" * 68)
    print("  Policy: export_customer_pii permitted only when scope contains 'admin'\n")

    for role in ("analyst", "admin"):
        print(f"── {role.upper()} (scope: {CLIENTS[role]['scope']}) ──")
        token = get_token(CLIENTS[role])
        tools = list_tools(token)
        print(f"   visible tools: {tools}")

        crm = call_tool(token, "crmTool___get_crm_customers", "msp-a")
        print(f"   get_crm_customers   -> {_verdict(crm)}")

        pii = call_tool(token, "piiTool___export_customer_pii", "msp-a")
        print(f"   export_customer_pii -> {_verdict(pii)}")
        print()


if __name__ == "__main__":
    main()
