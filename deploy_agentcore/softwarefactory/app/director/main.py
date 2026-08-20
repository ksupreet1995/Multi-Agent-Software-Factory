from typing import Any
from strands import Agent, tool
import asyncio
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
from memory.session import get_memory_session_manager
from factory_tools import get_crm_customers, execute_python
from specialists import delegate_to_code_writer, delegate_to_code_reviewer

app = BedrockAgentCoreApp()
log = app.logger

DEFAULT_SYSTEM_PROMPT = """
You are the Director agent of an automated "software factory". Engineers direct
you to produce reports for a specific MSP tenant. You do NOT write or review code
yourself — you orchestrate specialist agents that run on their own runtimes.

Workflow for a report request:
1. Determine the tenant_id from the request (default to 'msp-a' if unspecified).
2. Call get_crm_customers(tenant_id) to fetch that tenant's CRM data.
3. Call delegate_to_code_writer(task, crm_data_json) to have the Code Writer
   specialist generate a Python script that prints a "Customer Status Report"
   (total MRR, open support tickets, at-risk accounts). Pass the CRM data JSON.
4. Call delegate_to_code_reviewer(code) to have the Code Reviewer specialist
   check the script. If the verdict starts with REJECTED, ask the Code Writer
   to revise and review again (max 2 attempts).
5. Once APPROVED, call execute_python(code) to run it in the secure sandbox.
6. Present the report output to the engineer, and briefly note which specialist
   agents were involved.

Only ever use the tenant_id you were given. Never mix data across tenants.
"""

# The director orchestrates: CRM access, delegation to specialist runtimes,
# and sandboxed execution of the approved script.
tools = [
    get_crm_customers,
    delegate_to_code_writer,
    delegate_to_code_reviewer,
    execute_python,
]

_INLINE_FUNCTION_NAMES = set()


def _make_conversation_manager():
    return NullConversationManager()

def agent_factory():
    cache = {}
    def get_or_create_agent(session_id, user_id):
        _actor_id = user_id
        key = f"{session_id}/{_actor_id}"
        if key not in cache:
            cache[key] = Agent(
                model=load_model(),
                session_manager=get_memory_session_manager(session_id, _actor_id),
                conversation_manager=_make_conversation_manager(),
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                tools=tools,
                hooks=[
                ],
            )
        return cache[key]
    return get_or_create_agent
get_or_create_agent = agent_factory()


def _extract_prompt(payload: dict):
    """Accept harness-style messages[], tool_results[], or plain prompt string payloads."""
    if "messages" in payload:
        return payload["messages"]
    if "tool_results" in payload:
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in payload["tool_results"]]}]
    return payload.get("prompt", "")


def _has_inline_function_call(messages) -> bool:
    """Return True if messages contains an assistant toolUse for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES or not isinstance(messages, list):
        return False
    for msg in messages:
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("toolUse", {}).get("name") in _INLINE_FUNCTION_NAMES:
                    return True
    return False


def _is_inline_function_call(event: dict) -> bool:
    """Check if a contentBlockStart event is for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in _INLINE_FUNCTION_NAMES



@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")


    session_id = getattr(context, 'session_id', 'default-session')
    user_id = getattr(context, 'user_id', 'default-user')
    agent = get_or_create_agent(session_id, user_id)

    prompt = _extract_prompt(payload)

    # Honor the tenant selected by the caller. The payload carries tenantId
    # explicitly; bind it to the request so the director never falls back to a
    # default tenant (which would break tenant isolation).
    tenant_id = payload.get("tenantId")
    if tenant_id and isinstance(prompt, str):
        prompt = (
            f"[tenant_id={tenant_id}] {prompt}\n\n"
            f"Use tenant_id '{tenant_id}' for all CRM access and the report. "
            f"Do not use any other tenant."
        )
        log.info("Bound request to tenant_id=%s", tenant_id)


    async for event in agent.stream_async(
        prompt,
    ):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
