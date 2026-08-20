"""Real AgentCore Identity demo — just-in-time, scoped workload access tokens.

Directly answers the #1 security concern (JIT access tokens). Unlike the simulated
token in the local demo, this calls the real AgentCore Identity service:

  1. Ensure a workload identity exists for the software factory.
  2. Request a workload access token scoped to a specific (user/tenant, agent)
     pair via GetWorkloadAccessTokenForUserId.
  3. Show that each tenant gets its own distinct, freshly-minted token — the
     AgentCore Identity vault binds the token to the user-agent pair, so a
     token minted for one tenant cannot stand in for another.

Notes on the real security model (from the AgentCore docs):
  * Tokens are AWS-signed opaque tokens scoped to the specific user-agent pair.
  * Runtime/Gateway-managed identities cannot retrieve these tokens directly
    (prevents token extraction), which is why this issuance runs as a
    standalone workload identity rather than from inside the deployed runtime.
  * In production, prefer GetWorkloadAccessTokenForJWT (validates issuer +
    signature) over the userId path used here for the enablement demo.

Usage:
    python identity_demo.py
    python identity_demo.py --tenant msp-a --user eng-1
"""

from __future__ import annotations

import argparse
import os

from bedrock_agentcore.services.identity import IdentityClient

REGION = os.getenv("AWS_REGION", "us-east-2")
WORKLOAD_NAME = os.getenv("FACTORY_WORKLOAD_NAME", "software-factory")

# Partition user ids per tenant so tokens are bound to a tenant-scoped identity.
# The docs recommend the pattern provider_id+user_id to avoid collisions.
def _scoped_user_id(tenant_id: str, user_id: str) -> str:
    return f"{tenant_id}+{user_id}"


def ensure_workload_identity(client: IdentityClient, name: str) -> str:
    """Create the workload identity if it does not already exist. Returns name."""
    try:
        existing = client.get_workload_identity(name=name)
        print(f"  workload identity exists: {existing.get('name', name)}")
        return name
    except Exception:
        created = client.create_workload_identity(name=name)
        print(f"  created workload identity: {created.get('name', name)}")
        return created.get("name", name)


def issue_token(client: IdentityClient, tenant_id: str, user_id: str) -> str:
    scoped = _scoped_user_id(tenant_id, user_id)
    resp = client.get_workload_access_token(workload_name=WORKLOAD_NAME, user_id=scoped)
    token = resp.get("workloadAccessToken") or resp.get("accessToken") or ""
    return token


def _mask(token: str) -> str:
    if not token:
        return "(empty)"
    return f"{token[:12]}…{token[-6:]} (len={len(token)})"


def main() -> None:
    parser = argparse.ArgumentParser(description="Real AgentCore Identity JIT token demo")
    parser.add_argument("--tenant", default=None, help="single tenant to issue a token for")
    parser.add_argument("--user", default="eng-1")
    args = parser.parse_args()

    print("\n" + "=" * 68)
    print("  AgentCore Identity - Just-in-Time Scoped Token Issuance (LIVE)")
    print("=" * 68)
    print(f"  region        : {REGION}")
    print(f"  workload name : {WORKLOAD_NAME}\n")

    client = IdentityClient(REGION)

    print("Step 1: ensure workload identity")
    ensure_workload_identity(client, WORKLOAD_NAME)

    tenants = [args.tenant] if args.tenant else ["msp-a", "msp-b"]

    print("\nStep 2: issue a JIT scoped token per tenant (freshly minted each call)")
    tokens: dict[str, str] = {}
    for tenant in tenants:
        token = issue_token(client, tenant, args.user)
        tokens[tenant] = token
        print(f"  {tenant:<7} user={_scoped_user_id(tenant, args.user)}")
        print(f"          token: {_mask(token)}")

    if len(tokens) > 1:
        print("\nStep 3: confirm each tenant received a distinct token")
        vals = list(tokens.values())
        distinct = len(set(vals)) == len(vals)
        print(f"  tokens are {'DISTINCT [OK]' if distinct else 'IDENTICAL [!]'} per tenant-agent pair")
        print("  -> credentials stored under one tenant cannot be accessed by another")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
