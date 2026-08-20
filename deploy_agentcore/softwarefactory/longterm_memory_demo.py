"""Long-term Memory demo — extraction + tenant-namespace isolation.

Complements the deployed short-term memory (directorMemory). This creates a
standalone long-term memory with semantic + summary strategies whose namespaces
are TENANT-SCOPED, then proves:

  1. AgentCore asynchronously EXTRACTS durable facts/summaries from raw events.
  2. Namespaces ISOLATE per tenant — msp-a's extracted knowledge never surfaces
     when querying msp-b's namespace (the fine-grained-memory-isolation story).

Standalone + additive: does NOT touch the deployed director or its short-term
memory. Safe to run for the demo.

    python longterm_memory_demo.py            # full flow (create if needed, seed, extract, isolate)
    python longterm_memory_demo.py --retrieve # just retrieve (after extraction settled)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bedrock_agentcore.memory import MemoryClient

REGION = "us-east-2"
MEMORY_NAME = "software_factory_longterm_memory"
STATE = Path(__file__).parent / "_ltm_state.json"

client = MemoryClient(region_name=REGION)

# Tenant-scoped namespaces — this is the isolation boundary.
#   /facts/{tenant}      -> semantic facts, per tenant
#   /summaries/{tenant}  -> conversation summaries, per tenant
STRATEGIES = [
    {
        "semanticMemoryStrategy": {
            "name": "TenantFacts",
            "description": "Durable facts about a tenant's customers",
            "namespaces": ["/facts/{actorId}"],
        }
    },
    {
        "summaryMemoryStrategy": {
            "name": "TenantSummary",
            "description": "Per-tenant conversation summaries",
            "namespaces": ["/summaries/{actorId}/{sessionId}"],
        }
    },
]

# Seed conversations — actor_id IS the tenant, so namespaces resolve per tenant.
SEED = {
    "msp-a": [
        ("Which of our customers are at risk?", "USER"),
        ("Fabrikam Inc is at-risk with 7 open tickets and $1,500 MRR. "
         "Contoso Ltd is the largest account at $8,800 MRR. Total MRR is $20,800.", "ASSISTANT"),
    ],
    "msp-b": [
        ("Give me the account health summary.", "USER"),
        ("Proseware Systems is at-risk with 9 open tickets. Wingtip Toys is the "
         "largest at $9,600 MRR. Total MRR is $15,000 across 3 customers.", "ASSISTANT"),
    ],
}


def ensure_memory() -> str:
    if STATE.exists():
        mid = json.loads(STATE.read_text()).get("memory_id")
        if mid:
            print(f"  using existing memory: {mid}")
            return mid
    print("  creating long-term memory (semantic + summary, tenant namespaces)...")
    mem = client.create_memory_and_wait(
        name=MEMORY_NAME,
        strategies=STRATEGIES,
        description="Long-term memory demo — tenant-scoped namespaces",
        event_expiry_days=30,
    )
    mid = mem.get("memoryId") or mem.get("id")
    STATE.write_text(json.dumps({"memory_id": mid}))
    print(f"  created: {mid}")
    return mid


def seed(memory_id: str) -> None:
    for tenant, msgs in SEED.items():
        client.create_event(
            memory_id=memory_id, actor_id=tenant, session_id=f"{tenant}-ltm-seed", messages=msgs
        )
        print(f"  wrote {len(msgs)} events for {tenant}")


def retrieve(memory_id: str) -> None:
    print("\nRetrieval — semantic facts per tenant namespace:")
    for tenant in ("msp-a", "msp-b"):
        ns = f"/facts/{tenant}"
        mems = client.retrieve_memories(
            memory_id=memory_id, namespace=ns, query="at-risk customers and MRR", top_k=3
        )
        print(f"\n  namespace {ns}  ({len(mems)} memories):")
        for m in mems:
            text = (m.get("content", {}) or {}).get("text") or str(m)[:160]
            print(f"    - {text[:150]}")

    print("\nIsolation check — query msp-b's data from msp-a's namespace:")
    cross = client.retrieve_memories(
        memory_id=memory_id, namespace="/facts/msp-a", query="Proseware Wingtip Tailspin", top_k=3
    )
    leaked = any("proseware" in str(m).lower() or "wingtip" in str(m).lower() for m in cross)
    print(f"  msp-b names present in msp-a namespace? {'YES (leak!)' if leaked else 'NO — isolated [OK]'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieve", action="store_true", help="retrieve only (extraction already settled)")
    args = ap.parse_args()

    print("\n" + "=" * 66)
    print("  AgentCore Long-term Memory — extraction + tenant isolation")
    print("=" * 66)

    mid = ensure_memory()
    if args.retrieve:
        retrieve(mid)
        return

    print("\nSeeding tenant conversations...")
    seed(mid)
    print("\nWaiting for async long-term extraction (~30-60s)...")
    time.sleep(45)
    retrieve(mid)
    print("\nTip: if memories are empty, extraction is still running — "
          "re-run with --retrieve in a minute.\n")


if __name__ == "__main__":
    main()
