"""The one Prism scene owned by the host (prompt 2.7).

Scene loading is synchronous and belongs on the bridge's control thread.  It happens once while
an :class:`bridge.engine.EngineHost` is opening, before the audio shim exists, and therefore never
inside a session or an audio callback.  The engine itself refuses a second load on one handle.

This module deliberately does not expose a crossfade operation.  A crossfade restarts every loop
from sample zero, so prism-live keeps one scene for the lifetime of an engine handle.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "assets" / "scenes.json"
SCENE_ID = "prism-live"
STEM_ROLES = ("bed", "sub", "pulse", "air")


class SceneEngine(Protocol):
    scene: Path | None

    def load_scene(self, manifest: Path | str) -> None: ...


@dataclass(frozen=True)
class SceneSpec:
    """The checked, single scene described by a manifest."""

    manifest: Path
    scene_id: str
    stems: dict[str, Path]


@dataclass(frozen=True)
class SceneLoad:
    """One blocking control-thread load and its measured wall time."""

    spec: SceneSpec
    elapsed_ms: float


def read_manifest(manifest: Path | str = DEFAULT_MANIFEST) -> SceneSpec:
    """Read and enforce prism-live's one-scene/four-stem manifest contract."""
    path = Path(manifest).resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 1 or not isinstance(scenes[0], dict):
        raise ValueError(f"{path}: expected exactly one scene")
    scene = scenes[0]
    scene_id = scene.get("id")
    if not isinstance(scene_id, str) or document.get("default_scene") != scene_id:
        raise ValueError(f"{path}: the one scene must be default_scene")
    entries = scene.get("stems")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: stems must be a list")
    stems: dict[str, Path] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: every stem must be an object")
        role, filename = entry.get("role"), entry.get("file")
        if not isinstance(role, str) or not isinstance(filename, str):
            raise ValueError(f"{path}: every stem needs string role and file fields")
        if role in stems:
            raise ValueError(f"{path}: duplicate {role!r} stem")
        stems[role] = (path.parent / filename).resolve()
    if tuple(stems) != STEM_ROLES:
        raise ValueError(f"{path}: stems must be {', '.join(STEM_ROLES)} in that order")
    return SceneSpec(path, scene_id, stems)


def load_scene(
    engine: SceneEngine,
    manifest: Path | str = DEFAULT_MANIFEST,
    *,
    timer_ns=time.perf_counter_ns,
) -> SceneLoad:
    """Validate and synchronously load one manifest, returning the measured decode time."""
    spec = read_manifest(manifest)
    began = timer_ns()
    engine.load_scene(spec.manifest)
    elapsed_ms = (timer_ns() - began) / 1_000_000.0
    return SceneLoad(spec, elapsed_ms)
