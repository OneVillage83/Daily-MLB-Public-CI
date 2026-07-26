from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.redaction import redact_text  # noqa: E402
from app.stats.providers.pybaseball_compatibility import (  # noqa: E402
    run_installed_pybaseball_compatibility_probe,
)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def main() -> int:
    try:
        result = run_installed_pybaseball_compatibility_probe()
    except Exception as exc:
        sys.stderr.write(
            _json(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": redact_text(str(exc)),
                }
            )
            + "\n"
        )
        return 1
    sys.stdout.write(_json(result.as_dict()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
