from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.identifiers import generate_run_id, parse_requested_date  # noqa: E402
from app.jobs import JobRunner  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--date", required=True, help="Explicit report date in YYYY-MM-DD")
args = parser.parse_args()
settings.validate_for_collection()
requested_date = parse_requested_date(args.date)
db = Database(
    settings.database_path,
    busy_timeout_ms=settings.sqlite_busy_timeout_ms,
)
run_id = generate_run_id(requested_date)
db.create_run(run_id, requested_date, app_version=settings.app_version)
runner = JobRunner(settings)
try:
    runner.execute_now(run_id, requested_date)
finally:
    runner.shutdown(wait=True)
print(db.get_run(run_id))
