"""Code-based (deterministic) AgentCore evaluator: report_schema_check.

This is the *counterpart* to the LLM-as-a-judge evaluator ``report_quality``.
Where ``report_quality`` asks a Bedrock model "is this report good?", this
evaluator asks a purely mechanical question: **does the generated report
actually contain the required fields?** — with zero LLM inference, so it is
fully deterministic (same input -> same score, every run) and costs nothing
per evaluation.

It scores the director's SESSION output against a fixed schema for a customer
status report:

    1. Total MRR              (a dollar figure tied to MRR / recurring revenue)
    2. Open support tickets   (a count of open tickets)
    3. At-risk accounts       (a call-out of accounts flagged at risk)

Score = (fields present) / (fields required), so 0.0, 0.33, 0.67 or 1.0.
Label = "Pass" when all three are present, otherwise "Fail".

Implemented with the standard-library only. It follows the AgentCore
code-based-evaluator Lambda contract directly (parse the event, return a dict
with value/label/explanation), so the deployment bundle has no dependencies to
build — reinforcing the "tiny, deterministic, no-LLM" story.

Contract (see bedrock_agentcore.evaluation.custom_code_based_evaluators):
    event = {
        "evaluationLevel": "SESSION" | "TRACE" | "TOOL_CALL",
        "evaluationInput": {"sessionSpans": [ <ADOT span dict>, ... ]},
        "evaluationTarget": {"traceIds": [...], "spanIds": [...]},   # optional
        "evaluationReferenceInputs": [ ... ],                        # optional
    }
    return {"value": float, "label": str, "explanation": str}
"""

import json
import re

# Required report fields -> the deterministic signal that proves each is present.
# Each check is a list of regexes; the field counts as present if ANY match.
_REQUIRED_FIELDS = {
    "total_mrr": [
        r"total\s+mrr",
        r"mrr[^\n]{0,40}\$?\s?[\d,]+",
        r"\$\s?[\d,]+[^\n]{0,20}mrr",
        r"monthly\s+recurring\s+revenue",
    ],
    "open_support_tickets": [
        r"open\s+(support\s+)?tickets?",
        r"tickets?\s+open",
        r"support\s+tickets?[^\n]{0,40}\d+",
    ],
    "at_risk_accounts": [
        r"at[\s\-]?risk",
        r"accounts?\s+at\s+risk",
        r"churn\s+risk",
    ],
}

_FIELD_LABELS = {
    "total_mrr": "Total MRR",
    "open_support_tickets": "Open support tickets",
    "at_risk_accounts": "At-risk accounts",
}


def _extract_output_text(session_spans):
    """Concatenate all assistant / agent output text found in the session spans.

    The AgentCore evaluation service delivers ADOT spans. Strands emits the
    assistant response as a ``gen_ai.choice`` event (attribute ``message``) and
    also mirrors input/output onto span attributes. We gather every piece of
    output text we can find so the schema check works regardless of which shape
    the service sends.
    """
    chunks = []

    def _coerce(raw):
        """gen_ai content is often a JSON list of {"text": ...} parts."""
        if not raw:
            return
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw)
        text = str(raw)
        try:
            parts = json.loads(text)
            if isinstance(parts, list):
                joined = " ".join(
                    p.get("text", "") for p in parts if isinstance(p, dict)
                ).strip()
                chunks.append(joined or text)
                return
        except (ValueError, TypeError):
            pass
        chunks.append(text)

    for span in session_spans:
        if not isinstance(span, dict):
            continue

        # 1) gen_ai.choice events carry the assistant output.
        for event in span.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            name = event.get("name", "")
            attrs = event.get("attributes", {}) or {}
            if name == "gen_ai.choice" and attrs.get("message"):
                _coerce(attrs["message"])
            elif name in ("gen_ai.assistant.message", "gen_ai.choice") and attrs.get("content"):
                _coerce(attrs["content"])

        # 2) Common span attributes that mirror the output / response.
        attrs = span.get("attributes", {}) or {}
        for key in (
            "gen_ai.completion",
            "gen_ai.response",
            "agent.response",
            "output.value",
            "output",
            "response",
        ):
            if attrs.get(key):
                _coerce(attrs[key])

    return "\n".join(c for c in chunks if c)


def _evaluate(session_spans):
    output_text = _extract_output_text(session_spans)
    haystack = output_text.lower()

    present = {}
    for field, patterns in _REQUIRED_FIELDS.items():
        present[field] = any(re.search(p, haystack) for p in patterns)

    found = [f for f, ok in present.items() if ok]
    missing = [f for f, ok in present.items() if not ok]

    total = len(_REQUIRED_FIELDS)
    score = round(len(found) / total, 4)
    label = "Pass" if not missing else "Fail"

    if not output_text:
        explanation = (
            "No agent output text could be extracted from the session spans, "
            "so none of the required report fields could be verified."
        )
    else:
        found_str = ", ".join(_FIELD_LABELS[f] for f in found) or "none"
        missing_str = ", ".join(_FIELD_LABELS[f] for f in missing) or "none"
        explanation = (
            f"Deterministic schema check ({len(found)}/{total} required fields present). "
            f"Present: {found_str}. Missing: {missing_str}. "
            "No LLM was used — this is a mechanical field-presence check."
        )

    return {"value": float(score), "label": label, "explanation": explanation}


def handler(event, context=None):
    """AgentCore code-based evaluator Lambda entrypoint.

    Returns an EvaluatorOutput-shaped dict: {value, label, explanation}.
    On unexpected input it returns an error response instead of raising, so a
    malformed session never crashes the evaluation job.
    """
    try:
        evaluation_input = event.get("evaluationInput") or {}
        session_spans = evaluation_input.get("sessionSpans") or []
        return _evaluate(session_spans)
    except Exception as exc:  # noqa: BLE001 - never fail the eval job
        return {
            "errorCode": "EVALUATION_ERROR",
            "errorMessage": f"report_schema_check failed: {exc}",
        }
