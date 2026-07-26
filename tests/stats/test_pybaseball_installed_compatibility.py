from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.stats.providers.pybaseball_compatibility import (
    PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION,
    run_installed_pybaseball_compatibility_probe,
)


PYBASEBALL_INSTALLED = importlib.util.find_spec("pybaseball") is not None


@pytest.mark.skipif(
    not PYBASEBALL_INSTALLED,
    reason="optional locked statistics dependency profile is not installed",
)
def test_installed_pybaseball_227_executes_all_capabilities_offline() -> None:
    result = run_installed_pybaseball_compatibility_probe().as_dict()

    assert result["contract_version"] == PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION
    assert result["python_target"] == "3.12"
    assert result["pybaseball_version"] == "2.2.7"
    assert result["role"] == "noncanonical_parity_reference"
    assert result["canonical_transport"] is False
    assert result["offline"] is True
    assert result["network_requests"] == 0
    capabilities = result["capabilities"]
    assert set(capabilities) == {
        "batting_stats_range",
        "pitching_stats_range",
        "schedule_and_record",
        "statcast",
        "playerid_lookup",
        "playerid_reverse_lookup",
    }
    for capability in capabilities.values():
        assert capability["invoked"] is True
        assert capability["record_count"] == 1
        assert capability["canonical"] is False
    assert capabilities["batting_stats_range"]["baseball_reference_https"] is True
    assert capabilities["pitching_stats_range"]["baseball_reference_https"] is True
    assert capabilities["schedule_and_record"]["baseball_reference_https"] is True
    assert capabilities["statcast"]["parallel"] is False


@pytest.mark.skipif(
    not PYBASEBALL_INSTALLED,
    reason="optional locked statistics dependency profile is not installed",
)
def test_pybaseball_compatibility_cli_emits_deterministic_success_json() -> None:
    root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "scripts/check_pybaseball_compatibility.py"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["status"] == "passed"
    assert payload["network_requests"] == 0
    assert payload["pybaseball_version"] == "2.2.7"
