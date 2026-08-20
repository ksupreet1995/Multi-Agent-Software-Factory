"""AgentCore Agent Registry — publish & govern the Software Factory agents.

Registers the three agents we built (director, code_writer, code_reviewer) into
an AgentCore Registry, so they become discoverable, versioned, and governed —
the catalog layer for a "software factory" with many agents and engineers.

Flow demonstrated:
  1. Create a registry (with an approval workflow)
  2. Publish each deployed runtime as a registry record (CUSTOM descriptor)
  3. Submit the director for approval  ->  approve it (governance)
  4. Search the registry to discover agents

    python registry_demo.py
"""

from __future__ import annotations

import json
import time

import boto3

REGION = "us-east-2"
STATE = "agentcore/.cli/deployed-state.json"

c = boto3.client("bedrock-agentcore-control", region_name=REGION)


def _runtimes() -> dict:
    d = json.load(open(STATE))
    return d["targets"]["default"]["resources"]["runtimes"]


def _record_content(name: str, meta: dict, role: str) -> str:
    return json.dumps({
        "agent": name,
        "role": role,
        "runtimeArn": meta["runtimeArn"],
        "framework": "Strands",
        "protocol": "HTTP",
        "owner": "software-factory",
        "tools": meta.get("tools", []),
    }, indent=2)


AGENT_ROLES = {
    "director": "Orchestrator — plans the workflow and delegates to specialists.",
    "code_writer": "Specialist — generates Python for the requested report.",
    "code_reviewer": "Specialist — reviews generated code before execution.",
}


def ensure_registry(name: str = "software_factory_agent_registry") -> str:
    for r in c.list_registries().get("registrySummaries", c.list_registries().get("items", [])):
        if r.get("name") == name:
            rid = r.get("registryId") or r.get("id")
            print(f"  registry exists: {name} ({rid})")
            return rid
    resp = c.create_registry(
        name=name,
        description="Catalog of Software Factory agents",
        approvalConfiguration={"autoApproval": False},  # require explicit approval
    )
    rid = resp.get("registryId") or resp.get("id")
    print(f"  created registry: {name} ({rid})")
    return rid


def publish(registry_id: str) -> dict:
    runtimes = _runtimes()
    records = {}
    for name, meta in runtimes.items():
        role = AGENT_ROLES.get(name, "Agent")
        resp = c.create_registry_record(
            registryId=registry_id,
            name=name,
            description=role,
            descriptorType="CUSTOM",
            descriptors={"custom": {"inlineContent": _record_content(name, meta, role)}},
            recordVersion="1.0",
        )
        rec_id = resp.get("recordId") or resp.get("registryRecordId") or resp.get("id")
        records[name] = rec_id
        print(f"  published: {name:<14} -> record {rec_id}")
    return records


def govern(registry_id: str, record_id: str) -> None:
    print("\n[3/4] Governance: submit director for approval, then approve")
    try:
        c.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)
        print("  submitted director -> PENDING_APPROVAL")
    except Exception as e:  # noqa: BLE001
        print("  submit note:", str(e)[:120])
    time.sleep(2)
    try:
        c.update_registry_record_status(
            registryId=registry_id, recordId=record_id, status="APPROVED",
            statusReason="Reviewed for production use in the software factory.",
        )
        print("  approved director -> APPROVED (now production-eligible)")
    except Exception as e:  # noqa: BLE001
        print("  approve note:", str(e)[:120])


def discover(registry_id: str) -> None:
    print("\n[4/4] Discover: search the registry")
    dp = boto3.client("bedrock-agentcore", region_name=REGION)
    try:
        r = dp.search_registry_records(registryId=registry_id, maxResults=10)
        items = r.get("registryRecordSummaries", r.get("items", []))
        for it in items:
            print(f"  - {it.get('name'):<14} status={it.get('status','?')} "
                  f"v{it.get('recordVersion','?')}")
    except Exception as e:  # noqa: BLE001
        # fall back to control-plane list
        r = c.list_registry_records(registryId=registry_id)
        items = r.get("registryRecordSummaries", r.get("items", []))
        for it in items:
            print(f"  - {it.get('name'):<14} status={it.get('status','?')}")


def main() -> None:
    print("\n" + "=" * 66)
    print("  AgentCore Agent Registry — publish & govern the factory agents")
    print("=" * 66)
    print("\n[1/4] Create/ensure the registry")
    rid = ensure_registry()
    print("\n[2/4] Publish the deployed agents as registry records")
    records = publish(rid)
    if "director" in records:
        govern(rid, records["director"])
    discover(rid)
    print("\nDone. Agents are cataloged, versioned, and governed in the registry.\n")


if __name__ == "__main__":
    main()
