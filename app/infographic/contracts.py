from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.infographic.policy import InfographicPolicyV1
from app.redaction import redact_value

INFOGRAPHIC_DOCUMENT_CONTRACT_VERSION = "DSE_MLB_INFOGRAPHIC_DOCUMENT_V1"
INFOGRAPHIC_RENDER_VERSION = "DSE_MLB_INFOGRAPHIC_SVG_RENDER_V1"
INFOGRAPHIC_PHASE_INPUT_CONTRACT = "DSE_MLB_INFOGRAPHIC_PHASE_INPUT_V1"
INFOGRAPHIC_ATTEMPT_MANIFEST_CONTRACT = "DSE_INFOGRAPHIC_ATTEMPT_MANIFEST_V1"


class InfographicContractError(ValueError):
    pass


class InfographicVariantType(StrEnum):
    FEED_4X5 = "feed_4x5"
    STORY_9X16 = "story_9x16"


@dataclass(frozen=True, slots=True)
class InfographicSelectionV1:
    recommendation_rank: int
    source_game_id: str
    matchup_label: str
    selected_team_id: str
    selected_side: str
    prediction_probability: float
    market_probability: float | None
    edge: float | None
    expected_value_per_unit: float | None
    best_price: float | None
    bookmaker_count: int
    quality_disposition: str
    upstream_pdf_game_checksum: str
    upstream_ranking_entry_checksum: str
    upstream_gate_game_checksum: str
    upstream_value_checksum: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.recommendation_rank, bool)
            or not isinstance(self.recommendation_rank, int)
            or self.recommendation_rank < 1
        ):
            raise InfographicContractError("recommendation rank must be positive")
        if self.selected_side not in {"home", "away"}:
            raise InfographicContractError("selected side is invalid")
        for name in (
            "upstream_pdf_game_checksum",
            "upstream_ranking_entry_checksum",
            "upstream_gate_game_checksum",
            "upstream_value_checksum",
        ):
            value = getattr(self, name)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise InfographicContractError(f"{name} must be lowercase SHA-256")

    def identity_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class InfographicVariantV1:
    variant: InfographicVariantType
    width: int
    height: int
    pick_of_day: InfographicSelectionV1 | None
    recommendations: tuple[InfographicSelectionV1, ...]
    weather_headline: str
    weather_detail: str
    report_cta: str

    def __post_init__(self) -> None:
        expected = {InfographicVariantType.FEED_4X5: (1080, 1350), InfographicVariantType.STORY_9X16: (1080, 1920)}[
            self.variant
        ]
        if (self.width, self.height) != expected:
            raise InfographicContractError("variant dimensions mismatch")
        cards = (() if self.pick_of_day is None else (self.pick_of_day,)) + tuple(self.recommendations)
        ranks = [card.recommendation_rank for card in cards]
        if ranks != list(range(1, len(ranks) + 1)):
            raise InfographicContractError("infographic cards must retain canonical rank order")
        if self.pick_of_day is not None and self.pick_of_day.recommendation_rank != 1:
            raise InfographicContractError("Pick of the Day must be recommendation rank 1")

    def identity_dict(self) -> dict[str, object]:
        return {
            "height": self.height,
            "pick_of_day": None if self.pick_of_day is None else self.pick_of_day.as_dict(),
            "recommendations": [item.as_dict() for item in self.recommendations],
            "report_cta": self.report_cta,
            "variant": self.variant.value,
            "weather_detail": self.weather_detail,
            "weather_headline": self.weather_headline,
            "width": self.width,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class InfographicDocumentV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    generated_at: datetime
    upstream_pdf_report_snapshot_id: str
    upstream_pdf_report_checksum: str
    policy: InfographicPolicyV1
    variants: tuple[InfographicVariantV1, InfographicVariantV1]
    full_report_game_count: int
    full_report_recommendation_count: int
    warnings: tuple[Mapping[str, object], ...] = ()
    report_status: str = "pre_review"
    contract_version: str = INFOGRAPHIC_DOCUMENT_CONTRACT_VERSION
    render_version: str = INFOGRAPHIC_RENDER_VERSION
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        for name in ("as_of_time", "generated_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise InfographicContractError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if tuple(item.variant for item in self.variants) != tuple(InfographicVariantType):
            raise InfographicContractError("document requires feed then story variants")
        if self.report_status != "pre_review":
            raise InfographicContractError("Infographic remains pre_review")
        payload = self.identity_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured, preserve_field_names=("bookmaker_key", "market_key")) != payload:
            raise InfographicContractError("Infographic contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "full_report_game_count": self.full_report_game_count,
            "full_report_recommendation_count": self.full_report_recommendation_count,
            "generated_at": self.generated_at.isoformat(),
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "render_version": self.render_version,
            "report_status": self.report_status,
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_pdf_report_checksum": self.upstream_pdf_report_checksum,
            "upstream_pdf_report_snapshot_id": self.upstream_pdf_report_snapshot_id,
            "variants": [item.as_dict() for item in self.variants],
            "warnings": [dict(item) for item in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
