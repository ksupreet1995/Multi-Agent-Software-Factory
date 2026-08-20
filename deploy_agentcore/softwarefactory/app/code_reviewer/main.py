"""Code Reviewer specialist — its own AgentCore Runtime.

Part of the multi-agent "software factory": the director delegates a safety +
correctness review to this independently deployed runtime before any code is
executed. Responds with an APPROVED / REJECTED verdict.
"""

from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import (
    NullConversationManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

SYSTEM_PROMPT = """You are the Code Reviewer agent in an automated software
factory. Review the given Python for correctness and safety before it is
executed in a sandbox.

Respond in this exact format:
- First line: 'APPROVED' if it is safe and correct to execute, otherwise 'REJECTED'.
- Second line: a one-sentence reason.

Reject code that shells out (os.system, subprocess), deletes files, uses eval/
exec, makes network calls, or produces no stdout output. Be concise."""

_agent = None


def get_or_create_agent():
    global _agent
    if _agent is None:
        _agent = Agent(
            model=load_model(),
            system_prompt=SYSTEM_PROMPT,
            conversation_manager=NullConversationManager(),
        )
    return _agent


def _extract_prompt(payload: dict):
    if "messages" in payload:
        return payload["messages"]
    return payload.get("prompt", "")


@app.entrypoint
async def invoke(payload, context):
    log.info("Code Reviewer invoked.")
    agent = get_or_create_agent()
    prompt = _extract_prompt(payload)
    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
