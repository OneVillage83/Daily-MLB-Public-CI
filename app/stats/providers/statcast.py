from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.stats.contracts import (
    CsvRow,
    PybaseballParityHook,
    RawArtifact,
    StatcastQuery,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsTransport,
)
from app.stats.providers._common import (
    parse_csv_bytes,
    read_exact_capture,
    reject_block_page,
    require_content_type,
)

STATCAST_CSV_URL = "https://baseballsavant.mlb.com/statcast_search/csv"
STATCAST_REQUIRED_IDENTITY_COLUMNS = frozenset(
    {
        "game_pk",
        "game_date",
        "game_type",
        "at_bat_number",
        "pitch_number",
        "batter",
        "pitcher",
        "home_team",
        "away_team",
    }
)
STATCAST_REQUIRED_ANALYTICAL_COLUMNS = frozenset(
    {
        "away_score",
        "babip_value",
        "balls",
        "bb_type",
        "description",
        "des",
        "delta_home_win_exp",
        "delta_run_exp",
        "events",
        "estimated_ba_using_speedangle",
        "estimated_slg_using_speedangle",
        "estimated_woba_using_speedangle",
        "home_score",
        "inning",
        "inning_topbot",
        "iso_value",
        "launch_angle",
        "launch_speed",
        "launch_speed_angle",
        "outs_when_up",
        "p_throws",
        "pfx_x",
        "pfx_z",
        "pitch_name",
        "pitch_type",
        "plate_x",
        "plate_z",
        "post_away_score",
        "post_home_score",
        "release_extension",
        "release_speed",
        "release_spin_rate",
        "stand",
        "strikes",
        "woba_denom",
        "woba_value",
        "zone",
    }
)


@dataclass(frozen=True, slots=True)
class StatcastDataset:
    query: StatcastQuery
    columns: tuple[str, ...]
    rows: tuple[CsvRow, ...]
    raw: RawArtifact
    response_headers: Mapping[str, str]
    attempts: int
    parity_diagnostics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.parity_diagnostics is not None:
            object.__setattr__(
                self,
                "parity_diagnostics",
                MappingProxyType(dict(self.parity_diagnostics)),
            )
        object.__setattr__(
            self, "response_headers", MappingProxyType(dict(self.response_headers))
        )


class StatcastProvider:
    def __init__(
        self,
        transport: StatsTransport,
        *,
        url: str = STATCAST_CSV_URL,
        parity_hook: PybaseballParityHook | None = None,
    ) -> None:
        self.transport = transport
        self.url = url
        self.parity_hook = parity_hook

    def collect(
        self,
        query: StatcastQuery,
        *,
        fixture_key: str | None = None,
    ) -> StatcastDataset:
        response = self.transport.fetch(
            StatsRequest(
                provider=StatsProvider.STATCAST,
                endpoint_category="search_csv",
                fixture_key=fixture_key
                or (
                    f"statcast:{query.start_date.isoformat()}:"
                    f"{query.end_date.isoformat()}:{query.player_type}"
                ),
                url=self.url,
                params=query.request_params(),
                headers={"Accept": "text/csv, application/csv;q=0.9"},
                timeout_seconds=120.0,
                max_attempts=3,
            )
        )
        payload = read_exact_capture(response.capture)
        reject_block_page(payload, response.capture)
        require_content_type(
            response.capture,
            {
                "text/csv",
                "application/csv",
                "application/download",
                "application/octet-stream",
                "text/plain",
            },
        )
        if payload.lstrip().startswith((b"<", b"<!")):
            raise StatsProviderPayloadError(
                "Statcast returned HTML instead of CSV", capture=response.capture
            )
        columns, rows = parse_csv_bytes(
            payload,
            capture=response.capture,
            member_name="Statcast response",
        )
        missing = STATCAST_REQUIRED_IDENTITY_COLUMNS - set(columns)
        if missing:
            raise StatsProviderPayloadError(
                f"Statcast CSV is missing required columns {sorted(missing)}",
                capture=response.capture,
            )
        missing_analytical = STATCAST_REQUIRED_ANALYTICAL_COLUMNS - set(columns)
        if missing_analytical:
            raise StatsProviderPayloadError(
                "Statcast CSV is missing required analytical columns "
                f"{sorted(missing_analytical)}",
                capture=response.capture,
            )
        immutable_rows = tuple(MappingProxyType(dict(row)) for row in rows)

        parity_diagnostics = None
        if self.parity_hook is not None:
            parity_diagnostics = self.parity_hook(
                query,
                immutable_rows,
                response.capture.checksum_sha256,
            )
        return StatcastDataset(
            query=query,
            columns=columns,
            rows=immutable_rows,
            raw=response.capture,
            response_headers=response.headers,
            attempts=response.attempts,
            parity_diagnostics=parity_diagnostics,
        )
