"""Versioned MLB statistics acquisition and persistence primitives."""

from app.stats.repository import (
    InvalidStatsTransition,
    StatsInvariantError,
    StatsNotFoundError,
    StatsRepository,
    StatsRepositoryError,
)
from app.stats.player_snapshot_compatibility import (
    install_player_snapshot_payload_compatibility,
)
from app.stats.contracts import (
    FixtureResponse,
    RawArtifact,
    StatcastQuery,
    StatsError,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsRequestError,
    StatsResponse,
    StatsTransport,
    StatsTransportError,
)
from app.stats.raw_store import RawArtifactStore, RawStoreError
from app.stats.transport import FixtureStatsTransport, HttpStatsTransport


# Migration v6 moves Retrosheet fielding grain into dedicated columns. Install the
# deterministic compatibility adapter before acquisition services instantiate a
# repository so retained v5 payloads remain replay-idempotent after migration.
install_player_snapshot_payload_compatibility(StatsRepository)


__all__ = [
    "FixtureResponse",
    "FixtureStatsTransport",
    "HttpStatsTransport",
    "InvalidStatsTransition",
    "RawArtifact",
    "RawArtifactStore",
    "RawStoreError",
    "StatcastQuery",
    "StatsError",
    "StatsInvariantError",
    "StatsNotFoundError",
    "StatsProvider",
    "StatsProviderPayloadError",
    "StatsRepository",
    "StatsRepositoryError",
    "StatsRequest",
    "StatsRequestError",
    "StatsResponse",
    "StatsTransport",
    "StatsTransportError",
]
