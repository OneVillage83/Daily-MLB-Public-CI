import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.database import Database  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=settings.database_path,
        help="SQLite database to initialize or upgrade",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run SQLite integrity and foreign-key checks after migration",
    )
    args = parser.parse_args()
    database = Database(
        args.database,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    if args.check:
        database.verify_integrity()
    schema = database.schema_info()
    print(
        f"SQLite database ready at {database.path} "
        f"(schema_version={schema['version']}, integrity_checked={args.check})"
    )
    migration = database.migration_result
    if migration.migrated:
        print(
            f"Migration source={migration.source_kind} "
            f"backup={migration.backup_path or 'not-required'} "
            f"diagnostics={migration.diagnostic_path}"
        )
