from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable

from app.stats.contracts import CsvRow, RawArtifact, StatsProviderPayloadError

_BLOCK_MARKERS = (
    "access denied",
    "rate limit exceeded",
    "temporarily blocked",
    "verify you are human",
    "captcha",
    "cloudflare",
    "request unsuccessful",
)


def read_exact_capture(capture: RawArtifact) -> bytes:
    if capture.path.is_symlink():
        raise StatsProviderPayloadError(
            "Raw provider artifact became a symbolic link", capture=capture
        )
    payload = capture.path.read_bytes()
    if len(payload) != capture.size_bytes:
        raise StatsProviderPayloadError(
            "Raw provider artifact size changed before parsing", capture=capture
        )
    if hashlib.sha256(payload).hexdigest() != capture.checksum_sha256:
        raise StatsProviderPayloadError(
            "Raw provider artifact checksum changed before parsing", capture=capture
        )
    return payload


def verify_capture_file(capture: RawArtifact) -> None:
    if capture.path.is_symlink():
        raise StatsProviderPayloadError(
            "Raw provider artifact became a symbolic link", capture=capture
        )
    digest = hashlib.sha256()
    size = 0
    with capture.path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size != capture.size_bytes:
        raise StatsProviderPayloadError(
            "Raw provider artifact size changed before parsing", capture=capture
        )
    if digest.hexdigest() != capture.checksum_sha256:
        raise StatsProviderPayloadError(
            "Raw provider artifact checksum changed before parsing", capture=capture
        )


def read_capture_prefix(capture: RawArtifact, size: int = 256_000) -> bytes:
    verify_capture_file(capture)
    with capture.path.open("rb") as handle:
        return handle.read(size)


def require_content_type(
    capture: RawArtifact,
    allowed: Iterable[str],
) -> None:
    media_type = capture.content_type.partition(";")[0].strip().casefold()
    expected = frozenset(value.casefold() for value in allowed)
    if media_type not in expected:
        raise StatsProviderPayloadError(
            f"Unexpected {capture.provider.value} content type {media_type!r}",
            capture=capture,
        )


def reject_block_page(payload: bytes, capture: RawArtifact) -> None:
    sample = payload[:256_000].decode("utf-8", errors="ignore").casefold()
    marker = next((value for value in _BLOCK_MARKERS if value in sample), None)
    if marker is not None:
        raise StatsProviderPayloadError(
            f"Provider returned a block page ({marker})", capture=capture
        )


def parse_csv_bytes(
    payload: bytes,
    *,
    capture: RawArtifact,
    member_name: str,
) -> tuple[tuple[str, ...], tuple[CsvRow, ...]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StatsProviderPayloadError(
            f"{member_name} is not valid UTF-8 CSV", capture=capture
        ) from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fieldnames = reader.fieldnames
        if fieldnames is None or not fieldnames:
            raise StatsProviderPayloadError(
                f"{member_name} has no CSV header", capture=capture
            )
        normalized_fields = tuple(value.strip() for value in fieldnames)
        if any(not value for value in normalized_fields) or len(set(normalized_fields)) != len(
            normalized_fields
        ):
            raise StatsProviderPayloadError(
                f"{member_name} has blank or duplicate CSV columns", capture=capture
            )

        rows: list[CsvRow] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise StatsProviderPayloadError(
                    f"{member_name} row {line_number} has more values than columns",
                    capture=capture,
                )
            if any(value is None for value in row.values()):
                raise StatsProviderPayloadError(
                    f"{member_name} row {line_number} has fewer values than columns",
                    capture=capture,
                )
            rows.append(
                {
                    normalized_fields[index]: str(row[fieldnames[index]])
                    for index in range(len(fieldnames))
                }
            )
    except csv.Error as exc:
        raise StatsProviderPayloadError(
            f"{member_name} is malformed CSV", capture=capture
        ) from exc
    return normalized_fields, tuple(rows)
