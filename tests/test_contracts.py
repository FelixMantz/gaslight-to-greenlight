import json
import subprocess
import sys
from pathlib import Path

import pytest
import g2g.models as models
from g2g.state import TrustedEvent
from g2g.freeze import readiness
from g2g.util import ensure_local_runtime


def test_schema_export_command_matches_checked_in_files(tmp_path):
    subprocess.run(
        [sys.executable, "scripts/export_schemas.py", "--output", str(tmp_path)], check=True
    )
    expected = {p.name for p in Path("schemas").glob("*.schema.json")}
    assert {p.name for p in tmp_path.iterdir()} == expected
    for name in expected:
        assert json.loads((tmp_path / name).read_text()) == json.loads(Path("schemas", name).read_text())


@pytest.mark.parametrize(
    "contract",
    [
        models.VisiblePacket,
        models.AuditCase,
        models.GoldRecord,
        models.IdentityBinding,
        models.History,
        models.Request,
        models.Observation,
        models.BranchEvent,
        TrustedEvent,
    ],
)
def test_exported_schema_matches_executable_contract(contract):
    schema = json.loads(Path("schemas", contract.__name__ + ".schema.json").read_text())
    assert schema == contract.model_json_schema()


def test_paid_preset_and_synced_runtime_fail_closed(tmp_path, monkeypatch):
    import yaml

    config = yaml.safe_load(Path("config/study.yaml").read_text())
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CODESPACES", raising=False)
    config["price"] = {"version": "test", "input": 0, "output": 0}
    with pytest.raises(ValueError, match="Live gate"):
        readiness(tmp_path, config, "main")
    with pytest.raises(ValueError):
        ensure_local_runtime(tmp_path / "Dropbox/runtime", tmp_path / "project")
    with pytest.raises(ValueError):
        ensure_local_runtime(tmp_path / "project/runtime", tmp_path / "project")


def test_paid_work_is_rejected_in_ci(tmp_path, monkeypatch):
    import yaml

    config = yaml.safe_load(Path("config/study.yaml").read_text())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="sole local laptop"):
        readiness(tmp_path, config, "pilot")
