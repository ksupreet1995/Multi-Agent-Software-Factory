"""Core support for the LIVE web UI — config, CloudWatch logging, events, tenants.

This is the self-contained runtime the web UI needs to drive the DEPLOYED
AgentCore director. There is no local simulation ("MOCK") path — the UI always
invokes the real deployed runtimes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """Runtime configuration for the LIVE demo (env-driven)."""

    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-2"))
    model_id: str = field(
        default_factory=lambda: os.getenv(
            "MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
        )
    )

    # Live-resource names. Generic defaults ship in the repo; override via .env
    # (git-ignored) to point at your own deployment's real resource names.
    crm_lambda_name: str = field(
        default_factory=lambda: os.getenv("CRM_LAMBDA_NAME", "crm-backend")
    )
    ui_log_group: str = field(
        default_factory=lambda: os.getenv("UI_LOG_GROUP", "/software-factory/ui-demo")
    )
    # Directory name of the AgentCore CLI/CDK deployment project.
    deploy_project_dir: str = field(
        default_factory=lambda: os.getenv("DEPLOY_PROJECT_DIR", "softwarefactory")
    )


CONFIG = Config()


# ---------------------------------------------------------------------------
# CloudWatch logging
# ---------------------------------------------------------------------------
LOG_GROUP = CONFIG.ui_log_group


class CloudWatchLogger:
    """Best-effort CloudWatch Logs writer, safe to call from anywhere.

    Every UI workflow step is mirrored here so the run is observable in the AWS
    console. Degrades gracefully to a no-op if credentials/permissions are
    missing, so the demo never crashes on logging.
    """

    def __init__(self) -> None:
        self._client = None
        self._enabled = False
        self._status = "uninitialized"
        self._lock = threading.Lock()
        self._stream_created: set[str] = set()
        self._init_client()

    def _init_client(self) -> None:
        try:
            import boto3  # type: ignore

            self._client = boto3.client("logs", region_name=CONFIG.aws_region)
            try:
                self._client.create_log_group(logGroupName=LOG_GROUP)
            except self._client.exceptions.ResourceAlreadyExistsException:
                pass
            self._enabled = True
            self._status = f"connected ({CONFIG.aws_region})"
        except Exception as exc:  # noqa: BLE001 - never let logging break the demo
            self._enabled = False
            self._status = f"disabled: {type(exc).__name__}"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def status(self) -> str:
        return self._status

    @property
    def log_group(self) -> str:
        return LOG_GROUP

    def _ensure_stream(self, stream: str) -> None:
        if stream in self._stream_created:
            return
        try:
            self._client.create_log_stream(logGroupName=LOG_GROUP, logStreamName=stream)
        except Exception:  # noqa: BLE001 - already exists or transient
            pass
        self._stream_created.add(stream)

    def log(self, session_id: str, event_type: str, detail: dict[str, Any]) -> None:
        """Write one structured event to CloudWatch (best effort)."""
        if not self._enabled or self._client is None:
            return
        stream = f"session-{session_id}"
        message = json.dumps({"type": event_type, **detail}, default=str)
        with self._lock:
            try:
                self._ensure_stream(stream)
                self._client.put_log_events(
                    logGroupName=LOG_GROUP,
                    logStreamName=stream,
                    logEvents=[{"timestamp": int(time.time() * 1000), "message": message}],
                )
            except Exception:  # noqa: BLE001 - best effort
                pass


CLOUDWATCH = CloudWatchLogger()


# ---------------------------------------------------------------------------
# UI event
# ---------------------------------------------------------------------------
@dataclass
class FactoryEvent:
    """A UI event: pillar/step name, a human status, and an optional payload."""

    step: str          # e.g. "director", "gateway", "code_writer"
    status: str        # "running" | "done" | "error"
    title: str         # short label for the UI
    detail: str = ""   # human-readable line
    data: dict = field(default_factory=dict)  # structured payload (code, records, etc.)

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Tenants (the multi-tenant selector in the UI)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    display_name: str
    rate_limit_per_min: int


TENANTS: dict[str, Tenant] = {
    "msp-a": Tenant("msp-a", "MSP A (Northwind Group)", rate_limit_per_min=30),
    "msp-b": Tenant("msp-b", "MSP B (Tailspin Group)", rate_limit_per_min=5),
}
