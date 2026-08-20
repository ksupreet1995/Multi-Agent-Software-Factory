"""CRM backend Lambda — the external API exposed through AgentCore Gateway.

Gateway invokes this Lambda as an MCP tool target. The tool name and arguments
arrive in the event; we return per-tenant customer records. In production this
would query a real CRM (Salesforce, HubSpot, Supabase, etc.).

Gateway passes the tool context in the Lambda context's client_context and the
tool arguments as the event payload.
"""

from __future__ import annotations

import json

# Per-tenant CRM data. Gateway + Identity guarantee the caller only receives
# the tenant they are authorized for.
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


def _get_customers(tenant_id: str) -> list[dict]:
    records = sorted(
        _CRM_DATA.get(tenant_id, []), key=lambda r: r["mrr"], reverse=True
    )
    return records


# Sensitive PII per tenant — this tool is admin-only (enforced by AgentCore
# Policy / Cedar at the Gateway, not by this code).
_CRM_PII = {
    "msp-a": [
        {"name": "Northwind Traders", "billing_contact": "ada@northwind.example",
         "phone": "+1-555-0101", "account_number": "NW-88213"},
        {"name": "Fabrikam Inc", "billing_contact": "sam@fabrikam.example",
         "phone": "+1-555-0102", "account_number": "FB-40917"},
    ],
    "msp-b": [
        {"name": "Wingtip Toys", "billing_contact": "lee@wingtip.example",
         "phone": "+1-555-0201", "account_number": "WT-55012"},
        {"name": "Proseware Systems", "billing_contact": "kai@proseware.example",
         "phone": "+1-555-0202", "account_number": "PW-33188"},
    ],
}


def _export_pii(tenant_id: str) -> list[dict]:
    return _CRM_PII.get(tenant_id, [])


def handler(event, context):
    """Gateway Lambda target entrypoint.

    The tool name is provided by Gateway in the client context
    (bedrockAgentCoreToolName); arguments arrive in `event`.
    """
    tool_name = ""
    try:
        client_context = getattr(context, "client_context", None)
        if client_context and getattr(client_context, "custom", None):
            tool_name = client_context.custom.get("bedrockAgentCoreToolName", "")
    except Exception:  # noqa: BLE001
        tool_name = ""

    # Strip any gateway target prefix (e.g. "crmTarget___get_crm_customers").
    if "___" in tool_name:
        tool_name = tool_name.split("___", 1)[1]

    tenant_id = (event or {}).get("tenant_id", "")

    if tool_name in ("get_crm_customers", "") and tenant_id:
        return {"customers": _get_customers(tenant_id)}

    if tool_name == "export_customer_pii" and tenant_id:
        return {"pii_records": _export_pii(tenant_id)}

    return {
        "error": f"unknown tool '{tool_name}' or missing tenant_id",
        "received_event": event,
    }
