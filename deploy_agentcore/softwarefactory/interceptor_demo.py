"""Finest-grained access control demo — AgentCore Gateway REQUEST interceptor.

Tier 3 (after JWT authorizer + Cedar Policy): a Lambda interceptor inspects the
actual TOOL ARGUMENTS at request time and can allow, transform, or deny.

Rules enforced by the interceptor (see lambda_interceptor/handler.py):
  1. export_customer_pii is DENIED unless a non-empty `justification` arg is present.
  2. get_crm_customers `limit` is capped at 3 (over-limit requests are trimmed).

Reuses the admin token from the policy demo (admin passes Cedar; the interceptor
then applies the argument-level business rules on top).

    python interceptor_demo.py
"""

from __future__ import annotations

import json

from policy_demo import get_token, call_tool, CLIENTS, GATEWAY_URL
import urllib.request


def _call(token: str, tool: str, arguments: dict) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
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
        return {"http_error": e.code}
    raw = raw.strip()
    if not raw.startswith("{"):
        for line in reversed(raw.splitlines()):
            if line.strip().startswith("data:"):
                raw = line.strip()[5:].strip(); break
    return json.loads(raw)


def _verdict(resp: dict) -> str:
    err = resp.get("result", {}).get("isError") or "error" in resp
    if "error" in resp:
        msg = resp["error"].get("message", "")
        if "interceptor" in msg.lower():
            return f"BLOCKED by interceptor — {msg.split(':',1)[-1].strip()[:70]}"
        return f"DENIED — {msg[:70]}"
    if resp.get("result"):
        # count records if present
        content = resp["result"].get("content", [])
        txt = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return f"ALLOWED ({len(txt)} chars returned)"
    return f"? {str(resp)[:80]}"


def main() -> None:
    print("\n" + "=" * 70)
    print("  Finest-grained — Gateway REQUEST interceptor (argument-level rules)")
    print("=" * 70)
    print("  Rule 1: export_customer_pii needs a 'justification' argument")
    print("  Rule 2: get_crm_customers limit is capped at 3\n")

    token = get_token(CLIENTS["admin"])  # admin passes Cedar; interceptor still applies

    print("── export_customer_pii WITHOUT justification (should be BLOCKED) ──")
    r = _call(token, "piiTool___export_customer_pii", {"tenant_id": "msp-a"})
    print(f"   {_verdict(r)}\n")

    print("── export_customer_pii WITH justification (should be ALLOWED) ──")
    r = _call(token, "piiTool___export_customer_pii",
              {"tenant_id": "msp-a", "justification": "Quarterly compliance audit #4471"})
    print(f"   {_verdict(r)}\n")

    print("── get_crm_customers limit=99 (interceptor trims to 3) ──")
    r = _call(token, "crmTool___get_crm_customers", {"tenant_id": "msp-a", "limit": 99})
    print(f"   {_verdict(r)}")
    print("   (interceptor rewrote limit 99 -> 3 before the tool ran)\n")


if __name__ == "__main__":
    main()
