"""Lane registry: config `lanes: [{"kind": "claude", ...}]` -> Lane objects."""
from __future__ import annotations

import importlib

from .base import Lane, task_prompt  # noqa: F401

KINDS = {
    "mock": "relay.lanes.mock:MockLane",
    "claude": "relay.lanes.claude:ClaudeLane",
    "codex": "relay.lanes.codex:CodexLane",
    "relay": "relay.lanes.relay_api:RelayLane",
    "agent37": "relay.lanes.agent37:Agent37Lane",
    "orca": "relay.lanes.orca:OrcaLane",
}


def lane_class(kind: str) -> type[Lane]:
    mod, _, cls = KINDS[kind].partition(":")
    return getattr(importlib.import_module(mod), cls)


def build_lanes(cfg: dict, only: list[str] | None = None) -> list[Lane]:
    lanes = []
    for spec in cfg.get("lanes", []):
        if spec.get("enabled") is False:
            continue
        if only and spec.get("kind") not in only and spec.get("name") not in only:
            continue
        lanes.append(lane_class(spec["kind"])(spec.get("name"), {**cfg.get("lane_defaults", {}), **spec, "_root": cfg}))
    return lanes
