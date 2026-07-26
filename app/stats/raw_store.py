from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from app.stats.contracts import CaptureIdFactory, RawArtifact, StatsProvider

_SAFE_COMPONENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CAPTURE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CACHE_CONTRACT = "DSE_STATS_RAW_RESPONSE_CACHE_V1"
_CACHE_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-encoding", "date", "etag", "last-modified"}
)


class RawStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CachedRawResponse:
    artifact: RawArtifact
    status_code: int
    response_headers: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType(dict(self.response_headers)),
        )


def _default_capture_id() -> str:
    return uuid.uuid4().hex


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (path.exists() and path.resolve() != path.absolute())


class RawArtifactStore:
    """Writes exact provider bytes to unique immutable, checksummed artifacts."""

    def __init__(
        self,
        root: Path,
        *,
        capture_id_factory: CaptureIdFactory = _default_capture_id,
    ) -> None:
        self.root = Path(root)
        self.capture_id_factory = capture_id_factory
        if self.root.exists() and _is_link(self.root):
            raise RawStoreError("raw artifact root must not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link(self.root):
            raise RawStoreError("raw artifact root must not be a symbolic link")
        self._resolved_root = self.root.resolve(strict=True)

    def retain_bytes(
        self,
        *,
        provider: StatsProvider,
        endpoint_category: str,
        payload: bytes,
        retrieved_at: datetime,
        content_type: str,
        parent_checksum_sha256: str | None = None,
    ) -> RawArtifact:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        return self.retain_stream(
            provider=provider,
            endpoint_category=endpoint_category,
            chunks=(payload,),
            retrieved_at=retrieved_at,
            content_type=content_type,
            parent_checksum_sha256=parent_checksum_sha256,
        )

    def retain_stream(
        self,
        *,
        provider: StatsProvider,
        endpoint_category: str,
        chunks: Iterable[bytes],
        retrieved_at: datetime,
        content_type: str,
        parent_checksum_sha256: str | None = None,
    ) -> RawArtifact:
        endpoint = endpoint_category.strip()
        if _SAFE_COMPONENT_RE.fullmatch(endpoint) is None:
            raise RawStoreError("endpoint category is not a safe path component")
        if parent_checksum_sha256 is not None and _SHA256_RE.fullmatch(
            parent_checksum_sha256
        ) is None:
            raise RawStoreError("parent checksum must be lowercase SHA-256")
        normalized_content_type = content_type.strip()
        if not normalized_content_type or any(
            character in normalized_content_type for character in "\r\n"
        ):
            raise RawStoreError("content type must be a non-empty single-line value")

        captured_at = _utc(retrieved_at)
        capture_id = self.capture_id_factory()
        if _CAPTURE_ID_RE.fullmatch(capture_id) is None:
            raise RawStoreError("capture ID is not safe for an artifact filename")

        directory = self._contained_directory(
            provider.value, endpoint, captured_at.date().isoformat()
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".capture-", suffix=".part", dir=directory
        )
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("raw response chunks must be bytes")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            checksum = digest.hexdigest()
            filename = f"{capture_id}.bin"
            target = self._contained_path(directory, filename)
            self._publish_without_replace(temporary_path, target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        metadata_path = target.with_suffix(".json")
        metadata = {
            "capture_id": capture_id,
            "provider": provider.value,
            "endpoint_category": endpoint,
            "retrieved_at": captured_at.isoformat(),
            "content_type": normalized_content_type,
            "checksum_sha256": checksum,
            "size_bytes": size,
            "artifact_relpath": target.relative_to(self._resolved_root).as_posix(),
            "parent_checksum_sha256": parent_checksum_sha256,
        }
        metadata_bytes = json.dumps(
            metadata,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._write_metadata(metadata_path, metadata_bytes)

        return RawArtifact(
            capture_id=capture_id,
            provider=provider,
            endpoint_category=endpoint,
            retrieved_at=captured_at,
            content_type=normalized_content_type,
            checksum_sha256=checksum,
            size_bytes=size,
            path=target,
            metadata_path=metadata_path,
            parent_checksum_sha256=parent_checksum_sha256,
        )

    def read_verified(self, artifact: RawArtifact) -> bytes:
        target = self._contained_existing_path(artifact.path)
        payload = target.read_bytes()
        if len(payload) != artifact.size_bytes:
            raise RawStoreError("raw artifact size does not match metadata")
        if hashlib.sha256(payload).hexdigest() != artifact.checksum_sha256:
            raise RawStoreError("raw artifact checksum does not match metadata")
        return payload

    def load_verified_artifact(self, metadata_path: Path) -> RawArtifact:
        """Reconstruct an artifact from immutable metadata and verify exact bytes."""

        contained_metadata = self._contained_existing_path(metadata_path)
        artifact = self._artifact_from_metadata(contained_metadata)
        self.read_verified(artifact)
        return artifact

    def load_cached_response(
        self,
        *,
        provider: StatsProvider,
        request_fingerprint: str,
    ) -> CachedRawResponse | None:
        """Load an immutable cached response and re-verify its exact source bytes."""
        fingerprint = self._validated_fingerprint(request_fingerprint)
        index_path = self._cache_index_path(provider, fingerprint, create=False)
        if not index_path.exists():
            if index_path.is_symlink():
                raise RawStoreError("raw response cache index must not be a symbolic link")
            return None
        index_path = self._contained_existing_path(index_path)
        try:
            document = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RawStoreError("raw response cache index is malformed") from exc
        if not isinstance(document, dict):
            raise RawStoreError("raw response cache index is malformed")
        expected_identity = {
            "schema_version": _CACHE_CONTRACT,
            "provider": provider.value,
            "request_fingerprint": fingerprint,
        }
        if any(document.get(key) != value for key, value in expected_identity.items()):
            raise RawStoreError("raw response cache identity does not match its path")

        metadata_relpath = self._safe_relative_path(
            document.get("artifact_metadata_relpath"),
            field_name="cached artifact metadata path",
        )
        metadata_path = self._contained_existing_path(
            self._resolved_root.joinpath(*metadata_relpath.parts)
        )
        artifact = self._artifact_from_metadata(metadata_path)
        if artifact.provider is not provider:
            raise RawStoreError("cached artifact provider does not match cache identity")
        if document.get("endpoint_category") != artifact.endpoint_category:
            raise RawStoreError("cached artifact endpoint does not match cache identity")
        self.read_verified(artifact)

        status_code = document.get("status_code")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise RawStoreError("raw response cache contains a non-success status")
        headers = self._validated_cache_headers(document.get("response_headers"))
        return CachedRawResponse(
            artifact=artifact,
            status_code=status_code,
            response_headers=headers,
        )

    def publish_cached_response(
        self,
        *,
        provider: StatsProvider,
        request_fingerprint: str,
        artifact: RawArtifact,
        status_code: int,
        response_headers: Mapping[str, str],
    ) -> CachedRawResponse:
        """Atomically bind a validated request identity to immutable raw evidence."""
        fingerprint = self._validated_fingerprint(request_fingerprint)
        if artifact.provider is not provider:
            raise RawStoreError("cached artifact provider does not match request provider")
        if not 200 <= status_code < 300:
            raise RawStoreError("only successful provider responses may be cached")
        self.read_verified(artifact)
        metadata_path = self._contained_existing_path(artifact.metadata_path)
        reconstructed = self._artifact_from_metadata(metadata_path)
        if reconstructed != artifact:
            raise RawStoreError("raw artifact does not match its immutable metadata")
        headers = self._validated_cache_headers(dict(response_headers))
        index_path = self._cache_index_path(provider, fingerprint, create=True)
        document = {
            "artifact_metadata_relpath": metadata_path.relative_to(
                self._resolved_root
            ).as_posix(),
            "endpoint_category": artifact.endpoint_category,
            "provider": provider.value,
            "request_fingerprint": fingerprint,
            "response_headers": dict(headers),
            "schema_version": _CACHE_CONTRACT,
            "status_code": status_code,
        }
        payload = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if index_path.exists() or index_path.is_symlink():
            existing = self.load_cached_response(
                provider=provider,
                request_fingerprint=fingerprint,
            )
            if existing is None:
                raise RawStoreError("raw response cache disappeared during publication")
            return existing
        self._write_metadata(index_path, payload)
        cached = self.load_cached_response(
            provider=provider,
            request_fingerprint=fingerprint,
        )
        if cached is None:
            raise RawStoreError("raw response cache was not durable after publication")
        return cached

    def _artifact_from_metadata(self, metadata_path: Path) -> RawArtifact:
        try:
            document: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RawStoreError("raw artifact metadata is malformed") from exc
        if not isinstance(document, dict):
            raise RawStoreError("raw artifact metadata is malformed")
        required = {
            "capture_id",
            "provider",
            "endpoint_category",
            "retrieved_at",
            "content_type",
            "checksum_sha256",
            "size_bytes",
            "artifact_relpath",
            "parent_checksum_sha256",
        }
        if not required.issubset(document):
            raise RawStoreError("raw artifact metadata is incomplete")
        artifact_relpath = self._safe_relative_path(
            document["artifact_relpath"], field_name="raw artifact path"
        )
        try:
            provider = StatsProvider(str(document["provider"]))
            retrieved_at = datetime.fromisoformat(str(document["retrieved_at"]))
            size_bytes = int(document["size_bytes"])
        except (TypeError, ValueError) as exc:
            raise RawStoreError("raw artifact metadata contains invalid values") from exc
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise RawStoreError("raw artifact metadata timestamp is not timezone-aware")
        capture_id = str(document["capture_id"])
        endpoint = str(document["endpoint_category"])
        checksum = str(document["checksum_sha256"])
        content_type = str(document["content_type"])
        parent = document["parent_checksum_sha256"]
        if _CAPTURE_ID_RE.fullmatch(capture_id) is None:
            raise RawStoreError("raw artifact metadata capture ID is invalid")
        if _SAFE_COMPONENT_RE.fullmatch(endpoint) is None:
            raise RawStoreError("raw artifact metadata endpoint is invalid")
        if _SHA256_RE.fullmatch(checksum) is None or size_bytes < 0:
            raise RawStoreError("raw artifact metadata checksum or size is invalid")
        if not content_type.strip() or any(character in content_type for character in "\r\n"):
            raise RawStoreError("raw artifact metadata content type is invalid")
        if parent is not None and _SHA256_RE.fullmatch(str(parent)) is None:
            raise RawStoreError("raw artifact metadata parent checksum is invalid")
        artifact_path = self._contained_existing_path(
            self._resolved_root.joinpath(*artifact_relpath.parts)
        )
        if metadata_path != artifact_path.with_suffix(".json"):
            raise RawStoreError("raw artifact metadata path does not match artifact path")
        return RawArtifact(
            capture_id=capture_id,
            provider=provider,
            endpoint_category=endpoint,
            retrieved_at=retrieved_at,
            content_type=content_type,
            checksum_sha256=checksum,
            size_bytes=size_bytes,
            path=artifact_path,
            metadata_path=metadata_path,
            parent_checksum_sha256=str(parent) if parent is not None else None,
        )

    def _cache_index_path(
        self,
        provider: StatsProvider,
        fingerprint: str,
        *,
        create: bool,
    ) -> Path:
        directory = self._resolved_root / "cache" / provider.value
        if create:
            directory = self._contained_directory("cache", provider.value)
        else:
            self._assert_contained(directory.resolve(strict=False))
            if directory.exists() and _is_link(directory):
                raise RawStoreError("raw response cache directory must not be a symbolic link")
        path = directory / f"{fingerprint}.json"
        self._assert_contained(path.resolve(strict=False))
        return path

    @staticmethod
    def _validated_fingerprint(value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise RawStoreError("raw response cache fingerprint must be lowercase SHA-256")
        return value

    @staticmethod
    def _safe_relative_path(value: object, *, field_name: str) -> PurePosixPath:
        if not isinstance(value, str) or not value or "\\" in value:
            raise RawStoreError(f"{field_name} is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise RawStoreError(f"{field_name} is invalid")
        return path

    @staticmethod
    def _validated_cache_headers(value: object) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise RawStoreError("raw response cache headers are malformed")
        headers: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            name = str(raw_name).casefold()
            header_value = str(raw_value)
            if name not in _CACHE_RESPONSE_HEADERS:
                continue
            if any(character in name or character in header_value for character in "\r\n"):
                raise RawStoreError("raw response cache header is malformed")
            headers[name] = header_value
        return MappingProxyType(headers)

    def _contained_directory(self, *parts: str) -> Path:
        candidate = self._resolved_root.joinpath(*parts)
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
        self._assert_contained(resolved)
        current = self._resolved_root
        for part in parts:
            current = current / part
            if _is_link(current):
                raise RawStoreError("raw artifacts must not use symbolic-link directories")
        return resolved

    def _contained_path(self, parent: Path, filename: str) -> Path:
        candidate = parent / filename
        self._assert_contained(candidate.resolve(strict=False))
        if candidate.exists() or candidate.is_symlink():
            raise RawStoreError("raw artifact capture ID collision")
        return candidate

    def _contained_existing_path(self, path: Path) -> Path:
        lexical = Path(path)
        if lexical.is_symlink():
            raise RawStoreError("raw artifact must not be a symbolic link")
        resolved = lexical.resolve(strict=True)
        self._assert_contained(resolved)
        if not resolved.is_file():
            raise RawStoreError("raw artifact is not a regular file")
        return resolved

    def _assert_contained(self, path: Path) -> None:
        try:
            path.relative_to(self._resolved_root)
        except ValueError as exc:
            raise RawStoreError("raw artifact path escapes configured root") from exc

    @staticmethod
    def _publish_without_replace(temporary: Path, target: Path) -> None:
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise RawStoreError("raw artifact target already exists") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _write_metadata(self, target: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".metadata-", suffix=".part", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._publish_without_replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
