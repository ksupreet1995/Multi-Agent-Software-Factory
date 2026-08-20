"""Code Writer specialist — its own AgentCore Runtime.

Part of the multi-agent "software factory": the director delegates code
generation to this independently deployed runtime. Given a task plus CRM data,
it returns a single self-contained Python script (no prose, no fences).
"""

from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import (
    NullConversationManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

SYSTEM_PROMPT = """You are the Code Writer agent in an automated software factory.
Given a task and CRM data, output ONLY a single self-contained Python script
that solves it.

Hard rules:
- Output RAW Python source only. Your entire response must be valid Python that
  runs as-is with `python script.py`.
- Do NOT wrap the code in markdown fences (no ``` or ```python).
- Do NOT add any prose, explanation, or leading/trailing text.
- The very first character of your response must be Python code (e.g. an import,
  a comment `#`, or a statement) and the last must be the end of the script.
- The script must print its results to stdout using print().
- Use only the Python standard library. Do NOT make network calls, shell out
  (os.system/subprocess), use eval/exec, or touch the filesystem."""

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
    log.info("Code Writer invoked.")
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
