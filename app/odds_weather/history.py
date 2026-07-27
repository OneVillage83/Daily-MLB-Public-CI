from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol


class OddsHistorySelectionError(ValueError):
    """Raised when retained odds history cannot be selected safely at a PIT cutoff."""


class OddsHistorySource(Protocol):
    def get_odds_history(self, event_id: str) -> list[dict[str, Any]]: ...


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OddsHistorySelectionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _row_retrieved_at(row: Mapping[str, Any], index: int) -> datetime:
    raw = row.get("retrieved_at")
    if not isinstance(raw, str) or not raw.strip():
        raise OddsHistorySelectionError(
            f"odds history row {index} has no retrieved_at timestamp"
        )
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsHistorySelectionError(
            f"odds history row {index} has malformed retrieved_at timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OddsHistorySelectionError(
            f"odds history row {index} retrieved_at must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def select_odds_history_at(
    source: OddsHistorySource,
    *,
    provider_event_id: str,
    observed_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return retained odds rows known no later than ``observed_at``.

    The legacy database stores all observations for a sportsbook provider event. Phase 4
    must never hand later observations to ``calculate_line_movement`` when reconstructing
    an earlier point-in-time snapshot, so the cutoff is enforced here before the rows reach
    the frozen odds processor.
    """

    event_id = provider_event_id.strip()
    if not event_id:
        raise OddsHistorySelectionError("provider_event_id must be non-empty")
    cutoff = _aware_utc(observed_at, "observed_at")
    rows = source.get_odds_history(event_id)
    selected: list[tuple[datetime, int, dict[str, Any]]] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise OddsHistorySelectionError(
                f"odds history row {index} must be a mapping"
            )
        row = dict(raw_row)
        row_event_id = str(row.get("event_id") or "").strip()
        if row_event_id and row_event_id != event_id:
            raise OddsHistorySelectionError(
                f"odds history row {index} event_id disagrees with requested provider event"
            )
        retrieved_at = _row_retrieved_at(row, index)
        if retrieved_at > cutoff:
            continue
        raw_id = row.get("id")
        if raw_id is None:
            row_id = index
        elif isinstance(raw_id, bool):
            raise OddsHistorySelectionError(
                f"odds history row {index} id must be an integer when present"
            )
        else:
            try:
                row_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise OddsHistorySelectionError(
                    f"odds history row {index} id must be an integer when present"
                ) from exc
        selected.append((retrieved_at, row_id, row))

    selected.sort(key=lambda item: (item[0], item[1]))
    return tuple(row for _retrieved_at, _row_id, row in selected)
