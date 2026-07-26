from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


PHASE2_LIVE_WEATHER_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "PHASE2_LIVE_WEATHER_VALIDATION_REPORT.md"
)
PHASE2_ACCEPTANCE_MARKER = "PHASE2_RELEASE_GATE: ACCEPTED"
DEFAULT_NWS_USER_AGENT = "OneVillage-MLB-Research/1.0 (contact@example.com)"
SUPPORTED_OPENWEATHER_API_VERSIONS = frozenset({"3.0"})


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _placeholder_nws_user_agent(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        not normalized
        or normalized == DEFAULT_NWS_USER_AGENT.casefold()
        or "example.com" in normalized
        or "replace-with" in normalized
    )


@dataclass(frozen=True)
class Settings:
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    app_env: str = os.getenv("APP_ENV", "development")
    report_timezone: str = os.getenv("REPORT_TIMEZONE", "America/Los_Angeles")
    phase2_live_weather_accepted: bool = _bool("PHASE2_LIVE_WEATHER_ACCEPTED", False)
    phase2_weather_evidence_sha256: str = os.getenv(
        "PHASE2_WEATHER_EVIDENCE_SHA256", ""
    ).strip().lower()
    database_path: Path = Path(os.getenv("DATABASE_PATH", "./mlb_phase1.db"))
    artifact_dir: Path = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
    service_auth_token: str = field(default=os.getenv("SERVICE_AUTH_TOKEN", ""), repr=False)
    odds_api_key: str = field(default=os.getenv("ODDS_API_KEY", ""), repr=False)
    odds_regions: str = os.getenv("ODDS_REGIONS", "us")
    odds_markets: str = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")
    odds_format: str = os.getenv("ODDS_FORMAT", "american")
    odds_fresh_seconds: int = int(os.getenv("ODDS_FRESH_SECONDS", "120"))
    odds_stale_seconds: int = int(os.getenv("ODDS_STALE_SECONDS", "300"))
    odds_future_tolerance_seconds: int = int(
        os.getenv("ODDS_FUTURE_TOLERANCE_SECONDS", "30")
    )
    odds_consensus_min_books: int = int(os.getenv("ODDS_CONSENSUS_MIN_BOOKS", "2"))
    odds_consensus_moderate_books: int = int(
        os.getenv("ODDS_CONSENSUS_MODERATE_BOOKS", "4")
    )
    odds_consensus_high_books: int = int(
        os.getenv("ODDS_CONSENSUS_HIGH_BOOKS", "7")
    )
    odds_max_attempts: int = int(os.getenv("ODDS_MAX_ATTEMPTS", "3"))
    odds_retry_max_seconds: int = int(os.getenv("ODDS_RETRY_MAX_SECONDS", "30"))
    nws_user_agent: str = os.getenv("NWS_USER_AGENT", DEFAULT_NWS_USER_AGENT)
    openweather_enabled: bool = _bool("OPENWEATHER_ENABLED", True)
    openweather_api_key: str = field(default=os.getenv("OPENWEATHER_API_KEY", ""), repr=False)
    openweather_api_version: str = os.getenv("OPENWEATHER_API_VERSION", "3.0").strip()
    weather_compare_enabled: bool = _bool("WEATHER_COMPARE_ENABLED", True)
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    sqlite_busy_timeout_ms: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def credential_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.service_auth_token,
                self.odds_api_key,
                self.openweather_api_key,
            )
            if value
        )

    def phase2_weather_gate_clear(self) -> bool:
        checksum = self.phase2_weather_evidence_sha256
        if not (
            self.phase2_live_weather_accepted
            and len(checksum) == 64
            and all(character in "0123456789abcdef" for character in checksum)
        ):
            return False
        report_path = PHASE2_LIVE_WEATHER_REPORT_PATH
        try:
            if report_path.is_symlink():
                return False
            report_bytes = report_path.read_bytes()
            report_text = report_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if hashlib.sha256(report_bytes).hexdigest() != checksum:
            return False
        return PHASE2_ACCEPTANCE_MARKER in {
            line.strip() for line in report_text.splitlines()
        }

    def _weather_configuration_errors(self) -> list[str]:
        errors: list[str] = []
        if _placeholder_nws_user_agent(self.nws_user_agent):
            errors.append(
                "NWS_USER_AGENT must identify the application with real contact information"
            )
        if self.openweather_enabled and not self.openweather_api_key:
            errors.append("OPENWEATHER_API_KEY (or set OPENWEATHER_ENABLED=false)")
        if (
            self.openweather_enabled
            and self.openweather_api_version not in SUPPORTED_OPENWEATHER_API_VERSIONS
        ):
            errors.append(
                "OPENWEATHER_API_VERSION must be an explicitly supported version: "
                + ", ".join(sorted(SUPPORTED_OPENWEATHER_API_VERSIONS))
            )
        if self.request_timeout_seconds <= 0:
            errors.append("REQUEST_TIMEOUT_SECONDS must be greater than zero")
        try:
            ZoneInfo(self.report_timezone)
        except ZoneInfoNotFoundError:
            errors.append("REPORT_TIMEZONE must name an installed IANA timezone")
        if self.phase2_live_weather_accepted and not self.phase2_weather_gate_clear():
            errors.append(
                "PHASE2_WEATHER_EVIDENCE_SHA256 must identify the accepted Phase 2 report"
            )
        return errors

    def validate_for_weather(self) -> None:
        """Validate only the configuration needed by a bounded weather-only run."""
        errors = self._weather_configuration_errors()
        if errors:
            raise RuntimeError("Missing weather configuration: " + ", ".join(errors))

    def validate_for_collection(self) -> None:
        missing: list[str] = []
        if not self.odds_api_key:
            missing.append("ODDS_API_KEY")
        if not self.service_auth_token:
            missing.append("SERVICE_AUTH_TOKEN")
        missing.extend(self._weather_configuration_errors())
        if self.sqlite_busy_timeout_ms <= 0:
            missing.append("SQLITE_BUSY_TIMEOUT_MS must be greater than zero")
        if not 0 <= self.odds_fresh_seconds < self.odds_stale_seconds:
            missing.append(
                "ODDS_FRESH_SECONDS must be nonnegative and less than ODDS_STALE_SECONDS"
            )
        if self.odds_future_tolerance_seconds < 0:
            missing.append("ODDS_FUTURE_TOLERANCE_SECONDS must be nonnegative")
        if not (
            1
            <= self.odds_consensus_min_books
            <= self.odds_consensus_moderate_books
            <= self.odds_consensus_high_books
        ):
            missing.append("Odds consensus book thresholds must be positive and ordered")
        if self.odds_max_attempts < 1:
            missing.append("ODDS_MAX_ATTEMPTS must be at least one")
        if self.odds_retry_max_seconds < 0:
            missing.append("ODDS_RETRY_MAX_SECONDS must be nonnegative")
        if missing:
            raise RuntimeError("Missing configuration: " + ", ".join(missing))


settings = Settings()
