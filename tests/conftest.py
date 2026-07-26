import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", ":memory:")
os.environ.setdefault("ARTIFACT_DIR", "artifacts")
os.environ.setdefault("SERVICE_AUTH_TOKEN", "test-service-token-not-real")
os.environ.setdefault("ODDS_API_KEY", "test-odds-key-not-real")
os.environ.setdefault("OPENWEATHER_ENABLED", "false")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
