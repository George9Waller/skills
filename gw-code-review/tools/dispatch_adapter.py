"""Host-facing restricted dispatch contract and observation normalization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable, Protocol


LOOKUP_SCHEMA_VERSION = "review-lookup.v1"
LANE_TOOLS = frozenset({"get_change", "find_symbol", "get_source", "find_siblings", "grep_repo", "verify_anchor", "changed_file", "get_evidence_item", "match_pathspec"})
VERIFIER_TOOLS = LANE_TOOLS
CONFIGURED_MODEL = "haiku"


@dataclass(frozen=True)
class LaunchRequest:
    review_id: str
    lane_id: str
    phase: str
    model: str
    prompt_payload: str
    prompt_digest: str
    schema_version: str
    allowed_tools: frozenset[str]
    response_budget: int
    verification_id: str = ""
    candidate_ids: tuple[str, ...] = ()
    archive_dir: str = ""


@dataclass(frozen=True)
class LaunchObservation:
    lane_id: str
    phase: str
    model_id: str | None
    response_count: int | None
    tool_calls: tuple[str, ...]
    rejected_tool_calls: int
    aggregate_waits: int | None
    poll_wakeups: int | None
    prompt_digest: str | None
    tool_observation_exact: bool = True


class RestrictedHost(Protocol):
    """Minimum host API required for verified dispatch enforcement."""

    supports_hard_tool_allowlist: bool
    supports_model_observation: bool
    supports_response_observation: bool
    supports_wait_observation: bool
    supports_exact_tool_observation: bool

    def launch_restricted(self, request: LaunchRequest) -> LaunchObservation: ...


class DispatchAdapter:
    """One launch path that can enforce tools only on capable hosts."""

    def __init__(self, host: RestrictedHost, *, strict: bool = True) -> None:
        self.host = host
        self.strict = strict

    def capability_errors(self) -> list[str]:
        required = {
            "hard tool allowlist": self.host.supports_hard_tool_allowlist,
            "model observation": self.host.supports_model_observation,
            "response observation": self.host.supports_response_observation,
            "wait observation": self.host.supports_wait_observation,
            "exact tool observation": getattr(self.host, "supports_exact_tool_observation", False),
        }
        return [name for name, supported in required.items() if not supported]

    def launch(self, request: LaunchRequest) -> LaunchObservation:
        errors = self.capability_errors()
        if self.strict and errors:
            raise RuntimeError("restricted dispatch unavailable: " + ", ".join(errors))
        if request.model != CONFIGURED_MODEL:
            raise ValueError("dispatch model must be the configured Haiku model")
        if request.schema_version != LOOKUP_SCHEMA_VERSION:
            raise ValueError("dispatch lookup schema version does not match the restricted contract")
        expected_tools = VERIFIER_TOOLS if request.phase == "verifier" else LANE_TOOLS
        if request.phase not in {"lane", "verifier"}:
            raise ValueError("dispatch phase must be lane or verifier")
        if request.phase == "verifier" and (not request.verification_id or not request.candidate_ids):
            raise ValueError("verifier dispatch requires a verification ID and candidate IDs")
        if request.allowed_tools != expected_tools:
            raise ValueError("dispatch tool allowlist does not match the restricted contract")
        self._archive_launch(request)
        observation = self.host.launch_restricted(request)
        if observation.lane_id != request.lane_id or observation.phase != request.phase:
            raise ValueError("host observation does not match dispatched lane")
        if observation.prompt_digest != request.prompt_digest:
            raise ValueError("host observation does not attest the dispatched prompt")
        if self.strict and (observation.model_id is None or observation.response_count is None
                            or observation.aggregate_waits is None or observation.poll_wakeups is None):
            raise RuntimeError("restricted dispatch host returned incomplete observations")
        if observation.model_id is not None and observation.model_id != request.model:
            raise ValueError("host observation does not match the configured model")
        forbidden = set(observation.tool_calls) - expected_tools
        if observation.tool_observation_exact and forbidden:
            raise ValueError("host observation includes tools outside the restricted allowlist")
        return observation

    @staticmethod
    def parse_lane_result(raw_reply: str) -> dict:
        """Accept exactly one JSON object; never repair fences or prose."""
        if not isinstance(raw_reply, str) or not raw_reply.strip().startswith("{"):
            raise ValueError("lane result must be exactly one JSON object")
        decoder = json.JSONDecoder()
        try:
            value, end = decoder.raw_decode(raw_reply.lstrip())
        except json.JSONDecodeError as exc:
            raise ValueError("lane result is not valid JSON") from exc
        if raw_reply.lstrip()[end:].strip() or not isinstance(value, dict):
            raise ValueError("lane result must be exactly one JSON object")
        return value

    @staticmethod
    def _archive_launch(request: LaunchRequest) -> None:
        """Persist exact launch bytes only when a caller provides an archive directory."""
        if not request.archive_dir:
            return
        directory = Path(request.archive_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = request.prompt_payload.encode("utf-8")
        (directory / "payload.json").write_bytes(payload)
        (directory / "launch-prompt.txt").write_bytes(payload)
        (directory / "launch-artifact.json").write_text(json.dumps({
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
            "prompt_digest": request.prompt_digest,
        }, sort_keys=True), encoding="utf-8")

    def launch_and_record(self, request: LaunchRequest,
                          ingest: Callable[[str, list[dict], str], dict]) -> dict:
        """Launch through the enforcing host and atomically hand off verified facts.

        The callback boundary keeps this module independent of the MCP server
        while ensuring a host integration cannot accidentally relabel its own
        event as caller-supplied telemetry.
        """
        observation = self.launch(request)
        event = {
            "lane_id": observation.lane_id, "phase": observation.phase,
            "model_id": observation.model_id, "response_count": observation.response_count,
            "tool_calls": list(observation.tool_calls),
            "rejected_tool_calls": observation.rejected_tool_calls,
            "aggregate_waits": observation.aggregate_waits, "poll_wakeups": observation.poll_wakeups,
            "tool_observation_exact": observation.tool_observation_exact,
        }
        result = ingest(request.review_id, [event], "verified")
        if not result.get("ok"):
            raise RuntimeError("host observation was rejected by the review record")
        return result
