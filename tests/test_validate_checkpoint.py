from __future__ import annotations

from scripts.validate_checkpoint import SPRINT_FOCUSED_TESTS, _sqlite_gate, build_gates, main


def test_task_profile_requires_explicit_tests_and_python_surface() -> None:
    assert main(["task", "--explain"]) == 2


def test_task_profile_stays_focused() -> None:
    gates = build_gates(
        "task",
        python="python",
        stats_python="stats-python",
        tests=("tests/test_odds_weather_handler.py",),
        python_targets=("app/odds_weather/handler.py",),
    )

    assert [gate.label for gate in gates] == [
        "focused tests",
        "Ruff affected surface",
        "mypy affected surface",
    ]
    assert all("pip_audit" not in gate.command for gate in gates)
    assert all("verify_docker_release" not in " ".join(gate.command) for gate in gates)


def test_sprint_profile_runs_one_full_suite_and_focused_stats() -> None:
    gates = build_gates(
        "sprint",
        python="python",
        stats_python="stats-python",
        tests=(),
        python_targets=(),
    )

    labels = [gate.label for gate in gates]
    assert labels.count("development full suite") == 1
    assert labels.count("focused stats regression") == 1
    assert "stats full suite" not in labels
    assert "development locked audit" not in labels
    assert "Docker release verification" not in labels
    assert SPRINT_FOCUSED_TESTS


def test_release_profile_preserves_exhaustive_gates_without_task_promotion() -> None:
    gates = build_gates(
        "release",
        python="python",
        stats_python="stats-python",
        tests=(),
        python_targets=(),
    )
    labels = {gate.label for gate in gates}

    assert {
        "development full suite",
        "stats full suite",
        "development locked audit",
        "stats locked audit",
        "Docker release verification",
    }.issubset(labels)


def test_explain_prints_commands_without_execution(capsys: object) -> None:
    assert main(["sprint", "--explain"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "validation profile: sprint" in output
    assert "REQUIRED sprint focused tests" in output
    assert "SKIPPED: complete local stats-environment suite" in output


def test_sqlite_gate_resolves_repository_modules_and_validates_fresh_schema(
    capsys: object,
) -> None:
    _sqlite_gate()

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "integrity_check=ok foreign_key_check=0" in output
