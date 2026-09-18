import json
from pathlib import Path

import pytest

from bridge.scene import DEFAULT_MANIFEST, SCENE_ID, STEM_ROLES, load_scene, read_manifest


def test_the_production_manifest_is_one_default_scene_with_exactly_four_stems():
    spec = read_manifest()
    document = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    assert spec.scene_id == SCENE_ID == document["default_scene"]
    assert tuple(spec.stems) == STEM_ROLES
    assert "lead" not in spec.stems
    assert [path.name for path in spec.stems.values()] == [f"{role}.wav" for role in STEM_ROLES]
    assert {path.parent.name for path in spec.stems.values()} == {"placeholders"}


def test_scene_load_is_blocking_measured_and_delegated_once(tmp_path):
    manifest = _manifest(tmp_path)

    class FakeEngine:
        scene = None

        def __init__(self):
            self.loaded = []

        def load_scene(self, path):
            self.loaded.append(path)
            self.scene = Path(path)

    ticks = iter((2_000_000, 9_250_000))
    engine = FakeEngine()
    result = load_scene(engine, manifest, timer_ns=lambda: next(ticks))
    assert engine.loaded == [manifest.resolve()]
    assert result.elapsed_ms == 7.25


@pytest.mark.parametrize(
    "change, match",
    [
        (lambda d: d.update(default_scene="elsewhere"), "must be default_scene"),
        (lambda d: d["scenes"].append(dict(d["scenes"][0])), "exactly one scene"),
        (
            lambda d: d["scenes"][0]["stems"].append({"role": "lead", "file": "lead.wav"}),
            "stems must be",
        ),
    ],
)
def test_manifest_contract_rejects_scene_switching_and_lead(tmp_path, change, match):
    path = _manifest(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        read_manifest(path)


def _manifest(directory):
    for role in STEM_ROLES:
        (directory / f"{role}.wav").write_bytes(b"")
    document = {
        "schema_version": "1.0.0",
        "default_scene": "only",
        "scenes": [
            {
                "id": "only",
                "key": "D minor",
                "stems": [{"role": role, "file": f"{role}.wav"} for role in STEM_ROLES],
            }
        ],
    }
    path = directory / "scenes.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path
