# Fine-grained per-user tool access — AgentCore Policy (Cedar)

Demonstrates granting specific tools to specific users **within the same agent /
gateway**, enforced at the Gateway by Cedar policy — outside the agent's code,
immune to prompt injection.

## The scenario

One MCP gateway (`crm-policy-gw`), two tools, two users:

| User | OAuth scope | `get_crm_customers` | `export_customer_pii` |
|---|---|---|---|
| **analyst** | `invoke` | ✅ allowed | ❌ **denied** (tool not even visible) |
| **admin** | `invoke admin` | ✅ allowed | ✅ allowed |

The sensitive PII tool is restricted to callers whose JWT scope contains
`admin`. Everything else is default-deny.

## How it works

- **Cognito** issues per-user JWTs via M2M client_credentials. Two app clients
  (`analyst-client`, `admin-client`) carry different scopes from the `crm-api`
  resource server (`invoke` vs `invoke`+`admin`).
- **Gateway** (`crm-policy-gw`) uses **CUSTOM_JWT** auth. AgentCore parses the
  JWT → Cedar `principal` with a `scope` tag.
- **Policy engine** (`crm_policy_engine`) attached in **ENFORCE** mode holds two
  Cedar policies:
  - `crm_all_users` — permit `get_crm_customers` for any caller with `invoke` scope
  - `admin_only_pii` — permit `export_customer_pii` only when scope contains `admin`
- Default-deny does the rest: the analyst has no permit for the PII tool, so it
  is hidden from their tool list and blocked if called.

### The Cedar policies

```cedar
// crm_all_users
permit(
  principal,
  action == AgentCore::Action::"crmTool___get_crm_customers",
  resource == AgentCore::Gateway::"arn:...:gateway/crm-policy-gw"
) when { principal.hasTag("scope") && principal.getTag("scope") like "*invoke*" };

// admin_only_pii
permit(
  principal,
  action == AgentCore::Action::"piiTool___export_customer_pii",
  resource == AgentCore::Gateway::"arn:...:gateway/crm-policy-gw"
) when { principal.hasTag("scope") && principal.getTag("scope") like "*admin*" };
```

> Gotcha learned: AgentCore's Cedar evaluation needs a `when` scope guard on the
> permit. An unconditional `permit(principal, action==..., resource==...)` did
> not match (request fell through to default-deny). Guarding on `scope like
> "*invoke*"` fixed it.

## Run the demo

```powershell
python policy_demo.py
```

Output shows the analyst denied the PII tool (and it is absent from their tool
list) while the admin is allowed both tools.

## Show it in the AWS console

- **AgentCore console → Policy / Gateways** → `crm-policy-gw` → policy engine
  `crm_policy_engine` (ENFORCE): view the two Cedar policies.
- **Policy decision logs** in CloudWatch show each allow/deny decision with the
  principal, action, and matched policy — the audit trail.

## Layered security story

| Layer | Question | Mechanism |
|---|---|---|
| IAM | Can this principal call the Gateway API? | IAM (coarse, service-level) |
| **AgentCore Policy (Cedar)** | **Can THIS user use THIS tool?** | **Cedar permit/forbid (fine, tool-level)** |
| Gateway interceptors (Lambda) | Transform / redact request or response? | Lambda |

IAM says who reaches the gateway; **Policy says which tools each user gets** —
deterministic, human-readable, auditable, and enforced outside the agent.

## Resources created (us-east-2)

- Cognito user pool `crm-policy-demo` (`us-east-2_XXXXXXXXX`), domain `crm-policy-demo`
- Resource server `crm-api` (scopes: `invoke`, `admin`)
- App clients: `analyst-client`, `admin-client`
- Gateway `crm-policy-gw` (CUSTOM_JWT) with targets `crmTool`, `piiTool`
- Policy engine `crm_policy_engine` (ENFORCE) + policies `crm_all_users`, `admin_only_pii`
- Extended Lambda `crm-backend` with `export_customer_pii`
