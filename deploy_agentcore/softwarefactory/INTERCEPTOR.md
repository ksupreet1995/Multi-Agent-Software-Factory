# Finest-grained access control — Gateway REQUEST interceptor

Tier 3 of access control (after the JWT authorizer and Cedar Policy): a Lambda
interceptor inspects the actual **tool arguments** at request time and can
allow, transform, or deny the call before it reaches the target.

## Rules enforced (see `lambda_interceptor/handler.py`)

1. **`export_customer_pii` requires a `justification` argument** — a call
   without a non-empty justification is short-circuited (denied) by the
   interceptor. (Audit / accountability rule.)
2. **`get_crm_customers` `limit` is capped at 3** — an over-limit request is
   *rewritten* down to the cap before the tool runs. (Data minimization.)

This mirrors the AWS "allow if refund < $100" interceptor pattern.

## Proven live

```
export_customer_pii WITHOUT justification -> BLOCKED by interceptor
export_customer_pii WITH justification    -> ALLOWED
get_crm_customers limit=99                -> ALLOWED (interceptor trimmed 99 -> 3)
```

Run it:

```powershell
python interceptor_demo.py
```

## How it's wired

- Lambda `crm-interceptor` implements the MCP REQUEST interceptor contract
  (`interceptorInputVersion` / `interceptorOutputVersion` 1.0). It returns
  `transformedGatewayResponse` to deny, or `transformedGatewayRequest` to
  forward/rewrite.
- Attached to `crm-policy-gw` via `UpdateGateway`
  `interceptorConfigurations=[{interceptor:{lambda:{arn}}, interceptionPoints:[REQUEST]}]`.

### The gotcha that cost time

The gateway invokes the interceptor **using its own execution role**, not a
Lambda resource-based policy. Attaching the interceptor without granting the
gateway role `lambda:InvokeFunction` on the interceptor makes **every** request
(even `tools/list`) return HTTP 500, and the interceptor Lambda is never even
invoked (no logs). Fix:

```
put-role-policy on the gateway role (McpGatewayCrmPolicyGwRole):
  Allow lambda:InvokeFunction on arn:...:function:crm-interceptor
```

Then re-attach the interceptor and wait for READY + IAM propagation.

## The three-tier story

| Tier | Mechanism | Decides | Built |
|---|---|---|---|
| Fine | JWT authorizer | Is the identity/claim valid? (tenant_id) | ✅ |
| Finer | Cedar Policy | Can THIS user use THIS tool? | ✅ |
| Finest | Lambda interceptor | Is THIS call, with THESE arguments, allowed? | ✅ |

Layer them as governance needs grow — they compose, they don't compete.
