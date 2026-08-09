#!/usr/bin/env bash
set -Eeuo pipefail

# This verifier intentionally performs no collection requests. It exercises the
# production container's migration, HTTP, persistence, and security boundaries.
EXPECTED_GIT_SHA="${EXPECTED_GIT_SHA:-${GITHUB_SHA:-}}"
if [[ -z "$EXPECTED_GIT_SHA" ]]; then
  EXPECTED_GIT_SHA="$(git rev-parse HEAD)"
fi

OUTPUT_FILE="${VERIFICATION_OUTPUT:-.validation/docker-release/verification.json}"
OUTPUT_DIR="$(dirname "$OUTPUT_FILE")"
RUN_SUFFIX="${GITHUB_RUN_ID:-local}-$$"
IMAGE="daily-mlb:verification-${RUN_SUFFIX}"
CONTAINER="daily-mlb-verification-${RUN_SUFFIX}"
VOLUME="daily-mlb-verification-data-${RUN_SUFFIX}"
BUILD_LOG="${OUTPUT_DIR}/build.log"
RUNTIME_LOG="${OUTPUT_DIR}/runtime.log"
SCHEMA_BEFORE="${OUTPUT_DIR}/schema-before.json"
SCHEMA_AFTER="${OUTPUT_DIR}/schema-after.json"
OPENAPI_FILE="${OUTPUT_DIR}/openapi.json"
ROUTES_FILE="${OUTPUT_DIR}/routes.json"
HEALTH_FILE="${OUTPUT_DIR}/health.json"
COMPOSE_VALIDATION_DIR="${OUTPUT_DIR}/compose-validation"
COMPOSE_CONFIG_FILE="${COMPOSE_VALIDATION_DIR}/compose-config.json"

mkdir -p "$OUTPUT_DIR"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  rm -f "$BUILD_LOG" "$RUNTIME_LOG" "$SCHEMA_BEFORE" "$SCHEMA_AFTER" \
    "$OPENAPI_FILE" "$ROUTES_FILE" "$HEALTH_FILE"
  rm -rf "$COMPOSE_VALIDATION_DIR"
}
trap cleanup EXIT

fail() {
  printf 'Docker release verification failed: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command '$1' is unavailable"
}

require_command docker
require_command curl
require_command python3
require_command git

CHECKED_OUT_GIT_SHA="$(git rev-parse HEAD)"
[[ "$CHECKED_OUT_GIT_SHA" == "$EXPECTED_GIT_SHA" ]] \
  || fail "EXPECTED_GIT_SHA does not match the checked-out commit"
docker compose version >/dev/null 2>&1 \
  || fail "the Docker Compose v2 plugin is unavailable"

SERVICE_TOKEN="$(python3 -c 'import secrets; print("svc-" + secrets.token_hex(24))')"
ODDS_TOKEN="$(python3 -c 'import secrets; print("odds-" + secrets.token_hex(24))')"
WEATHER_TOKEN="$(python3 -c 'import secrets; print("weather-" + secrets.token_hex(24))')"

# Render the repository Compose defaults from an isolated copy with synthetic
# credentials. This proves the checked-in defaults without reading a developer's
# real .env or persisting the rendered secret-bearing configuration.
mkdir -p "$COMPOSE_VALIDATION_DIR"
cp docker-compose.yml "$COMPOSE_VALIDATION_DIR/docker-compose.yml"
{
  printf 'SERVICE_AUTH_TOKEN=%s\n' "$SERVICE_TOKEN"
  printf 'ODDS_API_KEY=%s\n' "$ODDS_TOKEN"
  printf 'OPENWEATHER_API_KEY=%s\n' "$WEATHER_TOKEN"
} >"$COMPOSE_VALIDATION_DIR/.env"
docker compose --project-directory "$COMPOSE_VALIDATION_DIR" \
  -f "$COMPOSE_VALIDATION_DIR/docker-compose.yml" config --format json \
  >"$COMPOSE_CONFIG_FILE"
python3 - "$COMPOSE_CONFIG_FILE" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
service = document["services"]["mlb-phase1"]
ports = service.get("ports", [])
assert len(ports) == 1
assert all(
    str(port.get("target")) == "8080"
    and str(port.get("published")) == "8080"
    and port.get("host_ip") == "127.0.0.1"
    for port in ports
)
volumes = service.get("volumes", [])
assert len(volumes) == 1
assert all(
    volume.get("type") == "volume"
    and volume.get("target") == "/data"
    and str(volume.get("source", "")).endswith("daily_mlb_data")
    for volume in volumes
)
environment = service.get("environment", {})
assert environment.get("DATABASE_PATH") == "/data/mlb_phase1.db"
assert environment.get("ARTIFACT_DIR") == "/data/artifacts"
assert service.get("restart") == "unless-stopped"
PY
rm -rf "$COMPOSE_VALIDATION_DIR"

scan_log_file() {
  local file="$1"
  local value
  for value in "$SERVICE_TOKEN" "$ODDS_TOKEN" "$WEATHER_TOKEN"; do
    if grep -Fq -- "$value" "$file"; then
      fail "a synthetic credential appeared in container output"
    fi
  done
  if grep -Eiq '(apiKey|api_key|authorization)[=:][^[:space:]]+' "$file"; then
    fail "credential-bearing request metadata appeared in container output"
  fi
}

if ! docker build --pull --build-arg "GIT_SHA=${EXPECTED_GIT_SHA}" --tag "$IMAGE" . >"$BUILD_LOG" 2>&1; then
  fail "image build did not succeed"
fi
scan_log_file "$BUILD_LOG"

IMAGE_REVISION="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE")"
[[ "$IMAGE_REVISION" == "$EXPECTED_GIT_SHA" ]] || fail "image revision does not match the checked-out commit"

CONFIGURED_USER="$(docker image inspect --format '{{.Config.User}}' "$IMAGE")"
[[ -n "$CONFIGURED_USER" && "$CONFIGURED_USER" != "root" && "$CONFIGURED_USER" != "0" ]] \
  || fail "image is not configured for non-root execution"

docker volume create "$VOLUME" >/dev/null

start_container() {
  docker run --detach \
    --name "$CONTAINER" \
    --mount "type=volume,src=${VOLUME},dst=/data" \
    --publish 127.0.0.1::8080 \
    --env APP_ENV=release-verification \
    --env REPORT_TIMEZONE=America/Los_Angeles \
    --env DATABASE_PATH=/data/mlb_phase1.db \
    --env ARTIFACT_DIR=/data/artifacts \
    --env "SERVICE_AUTH_TOKEN=${SERVICE_TOKEN}" \
    --env "ODDS_API_KEY=${ODDS_TOKEN}" \
    --env OPENWEATHER_ENABLED=false \
    --env "OPENWEATHER_API_KEY=${WEATHER_TOKEN}" \
    --env 'NWS_USER_AGENT=Daily-MLB-Docker-Verification/1.0 (verification@example.invalid)' \
    "$IMAGE" >/dev/null
}

wait_for_health() {
  local attempt status
  for attempt in $(seq 1 60); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$CONTAINER")"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER")" != "true" ]]; then
      fail "container exited before becoming healthy"
    fi
    sleep 1
  done
  fail "container did not become healthy within 60 seconds"
}

host_port() {
  docker inspect --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostPort}}' "$CONTAINER"
}

verify_runtime_identity() {
  local runtime_uid runtime_user app_owner_uid code_owner_uid host_ip
  runtime_uid="$(docker exec "$CONTAINER" id -u)"
  runtime_user="$(docker exec "$CONTAINER" id -un)"
  app_owner_uid="$(docker exec "$CONTAINER" stat -c '%u' /app)"
  code_owner_uid="$(docker exec "$CONTAINER" stat -c '%u' /app/app/main.py)"
  [[ "$runtime_uid" =~ ^[0-9]+$ && "$runtime_uid" -ne 0 ]] || fail "container process has root UID"
  [[ "$runtime_user" == "$CONFIGURED_USER" ]] || fail "runtime user does not match image configuration"
  [[ "$app_owner_uid" == "0" && "$code_owner_uid" == "0" ]] \
    || fail "application code is not root-owned"
  docker exec "$CONTAINER" test ! -w /app \
    || fail "application directory is writable by the runtime user"
  docker exec "$CONTAINER" test ! -w /app/app/main.py \
    || fail "application code is writable by the runtime user"
  docker exec "$CONTAINER" test -w /data \
    || fail "persistent data directory is not writable by the runtime user"
  host_ip="$(docker inspect --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostIp}}' "$CONTAINER")"
  [[ "$host_ip" == "127.0.0.1" ]] || fail "published service port is not localhost-only"
}

verify_stats_dependency_isolation() {
  docker exec "$CONTAINER" python -c \
    'from importlib.metadata import PackageNotFoundError, version; from pathlib import Path; names=("pybaseball", "pandas", "numpy", "scipy", "pyarrow", "matplotlib", "PyGithub", "beautifulsoup4"); found=[]
for name in names:
    try:
        found.append((name, version(name)))
    except PackageNotFoundError:
        pass
assert not found, f"stats-only distributions present: {found}"
assert not Path("/app/requirements-stats.txt").exists()'
}

capture_schema() {
  local target="$1"
  docker exec "$CONTAINER" python -c \
    'import json; from app.config import settings; from app.database import Database; db=Database(settings.database_path); print(json.dumps({"schema": db.schema_info(), "integrity": db.integrity_check()}, sort_keys=True))' \
    >"$target"
  python3 - "$target" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
schema = payload["schema"]
integrity = payload["integrity"]
assert schema["version"] == 14
assert schema["user_version"] == 14
assert schema["fingerprint"] == schema["expected_fingerprint"]
assert integrity["ok"] is True
assert integrity["integrity_check"] == ["ok"]
assert integrity["foreign_key_violations"] == []
PY
}

capture_logs() {
  docker logs "$CONTAINER" >"$RUNTIME_LOG" 2>&1
  scan_log_file "$RUNTIME_LOG"
}

start_container
wait_for_health
verify_runtime_identity
verify_stats_dependency_isolation

PORT="$(host_port)"
curl --fail --silent --show-error "http://127.0.0.1:${PORT}/health" >"$HEALTH_FILE"
curl --fail --silent --show-error "http://127.0.0.1:${PORT}/openapi.json" >"$OPENAPI_FILE"
docker exec "$CONTAINER" python -c \
  'import json; from app.main import app; print(json.dumps(sorted(({"path": str(route.path), "methods": sorted(route.methods or [])} for route in app.routes if getattr(route, "include_in_schema", False)), key=lambda item: item["path"])))' \
  >"$ROUTES_FILE"

python3 - "$HEALTH_FILE" "$OPENAPI_FILE" "$ROUTES_FILE" <<'PY'
import json
import sys

health = json.load(open(sys.argv[1], encoding="utf-8"))
document = json.load(open(sys.argv[2], encoding="utf-8"))
runtime_routes = json.load(open(sys.argv[3], encoding="utf-8"))
assert health == {"status": "ok", "environment": "release-verification"}
paths = set(document.get("paths", {}))
expected_methods = {
    "/health": {"GET"},
    "/jobs/daily-collection": {"POST"},
    "/jobs/{run_id}": {"GET"},
    "/jobs/{run_id}/artifact": {"GET"},
}
assert paths == set(expected_methods)
assert {
    route["path"]: set(route["methods"])
    for route in runtime_routes
} == expected_methods
PY

capture_schema "$SCHEMA_BEFORE"

docker exec "$CONTAINER" python -c \
  'from pathlib import Path; p=Path("/data/artifacts/docker-verification-marker.txt"); p.write_text("persistent-artifact\n", encoding="utf-8")'
docker exec "$CONTAINER" python -c \
  'import sqlite3; c=sqlite3.connect("/data/mlb_phase1.db"); assert c.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 0; c.close()'

# The human-review CLI is deliberately absent from the collection-service image.
docker exec "$CONTAINER" python -c \
  'from pathlib import Path; assert not Path("/app/scripts/release_candidate.py").exists()'

capture_logs
docker restart "$CONTAINER" >/dev/null
wait_for_health
verify_runtime_identity
docker exec "$CONTAINER" test -f /data/artifacts/docker-verification-marker.txt
capture_logs

docker stop "$CONTAINER" >/dev/null
docker rm "$CONTAINER" >/dev/null
start_container
wait_for_health
verify_runtime_identity
docker exec "$CONTAINER" test -f /data/artifacts/docker-verification-marker.txt
capture_schema "$SCHEMA_AFTER"
capture_logs

python3 - "$SCHEMA_BEFORE" "$SCHEMA_AFTER" <<'PY'
import json
import sys

before = json.load(open(sys.argv[1], encoding="utf-8"))
after = json.load(open(sys.argv[2], encoding="utf-8"))
assert after["schema"]["version"] == before["schema"]["version"]
assert after["schema"]["fingerprint"] == before["schema"]["fingerprint"]
assert after["schema"]["applied_at"] == before["schema"]["applied_at"]
assert after["integrity"]["ok"] is True
PY

IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
RUNTIME_UID="$(docker exec "$CONTAINER" id -u)"
HOST_IP="$(docker inspect --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostIp}}' "$CONTAINER")"
export EXPECTED_GIT_SHA CHECKED_OUT_GIT_SHA IMAGE_ID IMAGE_REVISION CONFIGURED_USER RUNTIME_UID HOST_IP

python3 - "$OUTPUT_FILE" "$SCHEMA_AFTER" "$ROUTES_FILE" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

schema_payload = json.load(open(sys.argv[2], encoding="utf-8"))
runtime_routes = json.load(open(sys.argv[3], encoding="utf-8"))
evidence = {
    "verification_version": "DSE_DOCKER_RELEASE_VERIFICATION_V1",
    "verified_at": datetime.now(timezone.utc).isoformat(),
    "commit_sha": os.environ["EXPECTED_GIT_SHA"],
    "checked_out_commit_sha": os.environ["CHECKED_OUT_GIT_SHA"],
    "image_id": os.environ["IMAGE_ID"],
    "image_revision": os.environ["IMAGE_REVISION"],
    "configured_user": os.environ["CONFIGURED_USER"],
    "runtime_uid": int(os.environ["RUNTIME_UID"]),
    "application_directory_writable": False,
    "application_code_owner_uid": 0,
    "application_code_writable": False,
    "persistent_data_directory_writable": True,
    "schema": schema_payload["schema"],
    "integrity": schema_payload["integrity"],
    "healthcheck": "healthy",
    "host_binding": os.environ["HOST_IP"],
    "compose_rendered_defaults_validated": True,
    "database_persisted_across_restart_and_recreation": True,
    "artifacts_persisted_across_restart_and_recreation": True,
    "http_routes": runtime_routes,
    "approval_or_publication_http_route_exposed": False,
    "release_cli_present_in_collection_image": False,
    "stats_dependencies_in_collection_image": False,
    "collection_runs_started": 0,
    "synthetic_credential_log_scan": "passed",
}
target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'Docker release verification passed for commit %s.\n' "$EXPECTED_GIT_SHA"
