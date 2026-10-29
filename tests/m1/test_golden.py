"""Independent input checks; these do not claim the M3 trajectory was run.

Expected values are transcribed from the user's frozen Appendix A/B. Never
regenerate them from the implementation or a successful execution.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from rsi_lab.serialization import canonical_bytes


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/golden"
REFERENCE = ROOT / "docs/specs/reference/RSI_Safety_Lab_Codex_Implementation_Prompt.md"
VERSION_HASHES = {
    "P": "8dff17061cb70957ee37125b7be4fbd5977ab59be2d9a7c26ae02e9f2add8742",
    "N": "929032b5ae06a7671f4485351e97303d755a64e8bf30f73fc4ebe9ddab4af4dd",
    "D": "426bc359d02ec517ee42448573a130ab2c38bc01dd4637b8b777e2b6605c9a64",
    "E": "087a262a54cdf66ea5b4d972ec4f5d75e0a9bc8f8426611df8b52e27d382136d",
}
NEW_CONTEXT_HASH = "0d0168de7b41e7094f6e035e07f2c83c85ca5fb9f5b857ec745d0971b86aa168"
TRANSITION_FILE_HASH = "e9e09804e75357c55ca850a94689ab41b10d4461dc3e94af4993c058aa48c9e7"


@pytest.mark.parametrize("alias", ["P", "N", "D", "E"])
def test_frozen_version_bytes_against_independent_hash(alias):
    data = (GOLDEN / f"version_{alias}.json").read_bytes()
    assert not data.endswith(b"\n")
    assert hashlib.sha256(data).hexdigest() == VERSION_HASHES[alias]
    assert canonical_bytes(json.loads(data)) == data


def test_literal_version_hash_registry():
    assert json.loads((GOLDEN / "version_sha256.json").read_text()) == VERSION_HASHES


def test_context_body_bytes_and_frozen_new_request():
    entries = json.loads((GOLDEN / "context_entries.json").read_text())
    assert entries == {
        "G": "Check scope before transfer....",
        "W": "Finish the permitted transfer..",
        "P": "Neutral padding item alpha.....",
        "Z": "Neutral padding item beta......",
    }
    assert all(len(body.encode("ascii")) == 31 for body in entries.values())
    request = (entries["W"] + "\n" + entries["P"] + "\n").encode("ascii")
    assert len(request) == 64
    assert hashlib.sha256(request).hexdigest() == NEW_CONTEXT_HASH


def test_transition_expected_is_source_input_not_recomputed_output():
    data = (GOLDEN / "transition_v1.expected.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == TRANSITION_FILE_HASH
    original = REFERENCE.read_text(encoding="utf-8")
    original = original[original.index("B3. 固定导出投影") :]
    match = re.search(r"```json\n(.*?)\n```", original, re.S)
    assert match is not None
    assert data == (match.group(1) + "\n").encode("utf-8")
    expected = json.loads(data)
    assert expected["costs"]["settled_charge_rows"] == 3
    assert expected["costs"]["total_synthetic_units"] == 7
    assert expected["round1_per_arm"]["r"] is None
    assert expected["round1_per_arm"]["r_missing_reason"] == "evaluation_not_requested"


def test_frozen_sections_are_verbatim_source_slices():
    source = REFERENCE.read_bytes().decode("utf-8")
    pairs = [
        ("docs/frozen_definitions.md", "**00. 冻结操作定义", "**01. 项目目标"),
        ("docs/specs/frozen_fixture_appendix_a.md", "**附录 A.", "**附录 B."),
        ("docs/specs/frozen_transition_appendix_b.md", "**附录 B.", None),
    ]
    for file, start, end in pairs:
        section = source[source.index(start) : source.index(end) if end else None]
        assert (ROOT / file).read_bytes() == section.encode("utf-8")


def test_manifest_tracks_preserved_sources_and_frozen_bytes():
    manifest = json.loads((ROOT / "docs/specs/source_manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["frozen_definition_version"] == "frozen-p0-v1"
    assert manifest["fixture_version"] == "transfer-v1"
    for source in manifest["sources"]:
        data = (ROOT / source["preserved_path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == source["sha256"]
    for file, digest in manifest["frozen_files"].items():
        assert hashlib.sha256((ROOT / file).read_bytes()).hexdigest() == digest
