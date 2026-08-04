from __future__ import annotations

import re
from pathlib import Path


DOCKERFILE = Path("Dockerfile")
COMPOSE = Path("docker-compose.yml")
ENTRYPOINT = Path("docker-entrypoint.sh")
WORKFLOW = Path(".github/workflows/quality.yml")
VERIFIER = Path("scripts/verify_docker_release.sh")
RELEASE_CLI = Path("scripts/release_candidate.py")
DOCKERIGNORE = Path(".dockerignore")


def test_image_is_locked_non_root_revision_labeled_and_migration_first() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert (
        "FROM python:3.12-slim@sha256:"
        "423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
        in dockerfile
    )
    assert "pip install --no-cache-dir --require-hashes -r requirements.txt" in dockerfile
    assert 'org.opencontainers.image.revision="${GIT_SHA}"' in dockerfile
    assert "USER mlb" in dockerfile
    assert "chown -R mlb:mlb /data" in dockerfile
    assert "chown -R root:root /app" in dockerfile
    assert "chmod -R a-w /app" in dockerfile
    assert "chmod 0750 /data /data/artifacts" in dockerfile
    assert "chown -R mlb:mlb /app" not in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY scripts/initialize_database.py" in dockerfile
    assert "COPY scripts ./scripts" not in dockerfile
    assert "initialize_database.py --check" in entrypoint
    assert entrypoint.index("initialize_database.py --check") < entrypoint.index("uvicorn")


def test_compose_defaults_to_localhost_and_persistent_data() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert '"127.0.0.1:8080:8080"' in compose
    assert "DATABASE_PATH: /data/mlb_phase1.db" in compose
    assert "ARTIFACT_DIR: /data/artifacts" in compose
    assert "daily_mlb_data:/data" in compose
    assert "volumes:\n  daily_mlb_data:" in compose


def test_ci_runs_real_docker_runtime_verification_for_rc_branches() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    exact_commit_expression = "${{ github.event.pull_request.head.sha || github.sha }}"

    assert '"rc/**"' in workflow
    assert '"phase1/**"' in workflow
    assert "bash scripts/verify_docker_release.sh" in workflow
    assert workflow.count(f"ref: {exact_commit_expression}") == 4
    assert f"EXPECTED_GIT_SHA: {exact_commit_expression}" in workflow
    assert f"docker-release-verification-{exact_commit_expression}" in workflow
    assert "EXPECTED_GIT_SHA: ${{ github.sha }}" not in workflow
    expected_action_pins = (
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2",
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
    )
    assert all(pin in workflow for pin in expected_action_pins)
    assert "python -m pip_audit --require-hashes -r requirements.txt" in workflow
    assert "python scripts/scan_repository_secrets.py --skip-env" in workflow
    assert "python -m pip install --require-hashes -r requirements-stats.txt" in workflow
    assert "python scripts/check_pybaseball_compatibility.py" in workflow
    assert "python -m pytest -q tests/stats" in workflow
    assert workflow.count("python -m pytest -q") == 4
    assert "linux-security:" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "Symlink containment and raw-evidence tests" in workflow
    linux_security_node_ids = (
        "tests/test_artifacts.py::"
        "test_resolve_contained_path_rejects_symlink_even_when_target_is_inside_root",
        "tests/test_artifacts.py::test_resolve_contained_path_rejects_symlink_to_outside_root",
        "tests/test_artifacts.py::test_write_json_rejects_symlink_target",
        "tests/test_artifacts.py::test_create_zip_rejects_symlink_source",
        "tests/test_artifacts.py::test_create_zip_rejects_symlink_run_directory",
        "tests/test_stats_evidence_secret_scan.py::test_symlink_is_not_followed",
    )
    assert all(node_id in workflow for node_id in linux_security_node_ids)
    assert "python -m pip_audit --require-hashes -r requirements-stats.txt" in workflow
    assert (
        "python -m pip install --dry-run --ignore-installed --require-hashes "
        "-r requirements-dev.txt"
    ) in workflow
    assert (
        "python -m pip install --dry-run --ignore-installed --require-hashes "
        "-r requirements-stats.txt"
    ) in workflow
    assert "docker build --tag daily-mlb:ci ." not in workflow

    uses_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
    assert uses_lines
    assert all(
        re.fullmatch(
            r"-?\s*uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}"
            r"\s+#\s+v\d+\.\d+\.\d+",
            line,
        )
        for line in uses_lines
    )


def test_docker_verifier_covers_release_boundaries_without_collection() -> None:
    source = VERIFIER.read_text(encoding="utf-8")

    required_fragments = (
        '--build-arg "GIT_SHA=${EXPECTED_GIT_SHA}"',
        'CHECKED_OUT_GIT_SHA="$(git rev-parse HEAD)"',
        "EXPECTED_GIT_SHA does not match the checked-out commit",
        "org.opencontainers.image.revision",
        "docker compose --project-directory",
        'config --format json',
        'port.get("host_ip") == "127.0.0.1"',
        'volume.get("target") == "/data"',
        'volume.get("type") == "volume"',
        'endswith("daily_mlb_data")',
        "id -u",
        "stat -c '%u' /app",
        "stat -c '%u' /app/app/main.py",
        "test ! -w /app",
        "test ! -w /app/app/main.py",
        "test -w /data",
        "127.0.0.1::8080",
        "/health",
        "/openapi.json",
        "routes.json",
        "app.routes",
        'paths == set(expected_methods)',
        "schema-before.json",
        "schema-after.json",
        'schema["version"] == 12',
        'schema["user_version"] == 13',
        "integrity_check",
        "foreign_key_violations",
        "docker restart",
        "docker-verification-marker.txt",
        "release_candidate.py",
        "collector_runs",
        "synthetic_credential_log_scan",
        "verify_stats_dependency_isolation",
        'names=("pybaseball", "pandas", "numpy", "scipy", "pyarrow",',
        'assert not Path("/app/requirements-stats.txt").exists()',
        '"stats_dependencies_in_collection_image": False',
    )
    for fragment in required_fragments:
        assert fragment in source

    assert source.count('"/jobs/daily-collection"') == 1
    assert "--request POST" not in source


def test_stats_dependency_profile_is_excluded_from_normal_image_context() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()

    assert "requirements-stats.in" not in dockerfile
    assert "requirements-stats.txt" not in dockerfile
    assert "requirements-stats.in" in dockerignore
    assert "requirements-stats.txt" in dockerignore


def test_local_release_cli_requires_explicit_human_review_inputs() -> None:
    source = RELEASE_CLI.read_text(encoding="utf-8")

    assert 'subparsers.add_parser("approve")' in source
    assert 'approve.add_argument("--reviewer-id", required=True)' in source
    assert 'approve.add_argument("--decisions", required=True)' in source
    assert "batch_review" in source
    assert "no public post performed" in source
