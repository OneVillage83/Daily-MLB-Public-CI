from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.identifiers import validate_run_id


ARCHIVE_FILENAME = "artifact.zip"
RAW_DIRECTORY_NAME = "raw"

ODDS_RAW_FILENAME = "odds_raw.json"
ODDS_RAW_ALL_UPCOMING_FILENAME = "odds_raw_all_upcoming.json"
GAMES_FILENAME = "games.json"
ODDS_CONSENSUS_FILENAME = "odds_consensus.json"
WEATHER_FILENAME = "weather.json"
COLLECTION_MANIFEST_FILENAME = "collection_manifest.json"
COLLECTION_ERRORS_FILENAME = "collection_errors.json"
CANONICAL_GAMES_FILENAME = "canonical_games.json"
ANALYSIS_FEATURES_FILENAME = "analysis_features.json"
SEALED_PREDICTIONS_FILENAME = "sealed_predictions.json"
PREDICTION_EVALUATIONS_FILENAME = "prediction_evaluations.json"
CANDIDATE_POLICY_RESULTS_FILENAME = "candidate_policy_results.json"
DAILY_CARD_JSON_FILENAME = "daily_card.json"
DAILY_CARD_MARKDOWN_FILENAME = "daily_card.md"
PUBLICATION_MANIFEST_FILENAME = "publication_manifest.json"
RESULTS_LEDGER_FILENAME = "results_ledger.json"

FIXED_JSON_FILENAMES = frozenset(
    {
        ODDS_RAW_FILENAME,
        ODDS_RAW_ALL_UPCOMING_FILENAME,
        GAMES_FILENAME,
        ODDS_CONSENSUS_FILENAME,
        WEATHER_FILENAME,
        COLLECTION_MANIFEST_FILENAME,
        COLLECTION_ERRORS_FILENAME,
        CANONICAL_GAMES_FILENAME,
        ANALYSIS_FEATURES_FILENAME,
        SEALED_PREDICTIONS_FILENAME,
        PREDICTION_EVALUATIONS_FILENAME,
        CANDIDATE_POLICY_RESULTS_FILENAME,
        DAILY_CARD_JSON_FILENAME,
        PUBLICATION_MANIFEST_FILENAME,
        RESULTS_LEDGER_FILENAME,
    }
)

_RAW_SEGMENT_PATTERN = re.compile(r"[a-z0-9_]+\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class UnsafeArtifactPath(ValueError):
    pass


def is_link_like(path: Path) -> bool:
    """Return true for symbolic links and Windows directory junctions."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and bool(is_junction()))


def _absolute_lexical(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _canonical_root(root: str | os.PathLike[str]) -> Path:
    root_path = _absolute_lexical(Path(root))
    if is_link_like(root_path):
        raise UnsafeArtifactPath("artifact root must not be a symbolic link")
    if root_path.exists() and not root_path.is_dir():
        raise UnsafeArtifactPath("artifact root must be a directory")
    return root_path.resolve(strict=False)


def _validate_relative_path(relative_path: str | os.PathLike[str]) -> Path:
    text = os.fspath(relative_path)
    if not isinstance(text, str) or not text or "\x00" in text:
        raise UnsafeArtifactPath("artifact path must be a non-empty relative path")
    if "%" in text:
        raise UnsafeArtifactPath("percent-encoded artifact paths are not allowed")
    if ":" in text:
        raise UnsafeArtifactPath("drive-qualified and alternate-stream paths are not allowed")

    windows_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root or posix_path.is_absolute():
        raise UnsafeArtifactPath("absolute, drive-qualified, and UNC artifact paths are not allowed")
    if not windows_path.parts or not posix_path.parts:
        raise UnsafeArtifactPath("artifact path must identify a child of the configured root")
    if any(part in {"", ".", ".."} for part in (*windows_path.parts, *posix_path.parts)):
        raise UnsafeArtifactPath("artifact path traversal is not allowed")
    if os.name != "nt" and "\\" in text:
        raise UnsafeArtifactPath("backslash-separated artifact paths are not allowed")
    return Path(text)


def resolve_contained_path(
    root: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
) -> Path:
    """Resolve a relative artifact path and prove it remains below ``root``."""
    root_lexical = _absolute_lexical(Path(root))
    root_resolved = _canonical_root(root_lexical)
    relative = _validate_relative_path(relative_path)

    candidate_lexical = root_lexical / relative
    current = root_lexical
    for part in relative.parts:
        current = current / part
        if is_link_like(current):
            raise UnsafeArtifactPath("symbolic links are not allowed in artifact paths")

    candidate_resolved = candidate_lexical.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeArtifactPath("artifact path escapes the configured root") from exc
    return candidate_resolved


def _validate_raw_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or _RAW_SEGMENT_PATTERN.fullmatch(value) is None:
        raise UnsafeArtifactPath(f"{label} must contain only lowercase letters, digits, and underscores")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    root: Path
    requested_date: date
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.requested_date, date) or isinstance(self.requested_date, datetime):
            raise TypeError("requested_date must be a datetime.date")
        canonical_root = _canonical_root(self.root)
        canonical_run_id = validate_run_id(self.run_id, expected_date=self.requested_date)
        object.__setattr__(self, "root", canonical_root)
        object.__setattr__(self, "run_id", canonical_run_id)

    @property
    def date_dir(self) -> Path:
        return resolve_contained_path(self.root, self.requested_date.isoformat())

    @property
    def run_dir(self) -> Path:
        relative = Path(self.requested_date.isoformat()) / self.run_id
        return resolve_contained_path(self.root, relative)

    @property
    def archive_path(self) -> Path:
        relative = Path(self.requested_date.isoformat()) / self.run_id / ARCHIVE_FILENAME
        return resolve_contained_path(self.root, relative)

    def json_path(self, filename: str) -> Path:
        if filename not in FIXED_JSON_FILENAMES:
            raise UnsafeArtifactPath("unsupported JSON artifact filename")
        relative = Path(self.requested_date.isoformat()) / self.run_id / filename
        return resolve_contained_path(self.root, relative)

    def text_path(self, filename: str) -> Path:
        if filename != DAILY_CARD_MARKDOWN_FILENAME:
            raise UnsafeArtifactPath("unsupported text artifact filename")
        relative = Path(self.requested_date.isoformat()) / self.run_id / filename
        return resolve_contained_path(self.root, relative)

    def raw_json_path(self, provider: str, endpoint_category: str, checksum: str) -> Path:
        safe_provider = _validate_raw_segment(provider, "provider")
        safe_category = _validate_raw_segment(endpoint_category, "endpoint_category")
        if not isinstance(checksum, str) or _SHA256_PATTERN.fullmatch(checksum) is None:
            raise UnsafeArtifactPath("checksum must be exactly 64 lowercase hexadecimal characters")
        relative = (
            Path(self.requested_date.isoformat())
            / self.run_id
            / RAW_DIRECTORY_NAME
            / safe_provider
            / safe_category
            / f"{checksum}.json"
        )
        return resolve_contained_path(self.root, relative)
