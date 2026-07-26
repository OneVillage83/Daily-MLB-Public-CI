from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from typing import Any


_FIELDING_GRAIN_METADATA_KEYS = frozenset(
    {"source_row_key", "position_code", "source_stint_key"}
)
_INSTALL_MARKER = "__dse_fielding_grain_payload_compatibility__"


def _legacy_compatible_record(
    record: Mapping[str, object],
) -> Mapping[str, object]:
    """Keep schema-v6 grain metadata outside the immutable statistics payload.

    Migration v5 stored the provider statistics object without the schema-v6
    source-row fields. Retained-source replay must therefore canonicalize the same
    payload after migration. The durable grain remains available through the
    dedicated ``source_row_key``, ``position_code``, and ``source_stint_key``
    columns on the record itself.
    """

    raw_stats = record.get("stats")
    if not isinstance(raw_stats, Mapping):
        return record
    if not any(key in raw_stats for key in _FIELDING_GRAIN_METADATA_KEYS):
        return record

    sanitized_stats = {
        key: value
        for key, value in raw_stats.items()
        if key not in _FIELDING_GRAIN_METADATA_KEYS
    }
    return {**record, "stats": sanitized_stats}


def install_player_snapshot_payload_compatibility(
    repository_type: type[Any],
) -> None:
    """Install the v5-to-v6 payload compatibility adapter exactly once."""

    original: Any = repository_type._game_player_snapshot_values
    if bool(getattr(original, _INSTALL_MARKER, False)):
        return

    @wraps(original)
    def compatible_values(
        self: Any,
        record: Mapping[str, object],
    ) -> dict[str, object]:
        return original(self, _legacy_compatible_record(record))

    setattr(compatible_values, _INSTALL_MARKER, True)
    setattr(repository_type, "_game_player_snapshot_values", compatible_values)
