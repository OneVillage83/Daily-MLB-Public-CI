from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_NAMES = {
    "accesstoken",
    "apikey",
    "appid",
    "authorization",
    "clientsecret",
    "credential",
    "credentials",
    "key",
    "password",
    "passwd",
    "refreshtoken",
    "secret",
    "serviceauthtoken",
    "sig",
    "signature",
    "token",
}
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"\bauthorization\s*[:=]\s*[^\r\n,;]+", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_LABELED_SECRET_RE = re.compile(
    r"(?P<prefix>\b(?:(?:[a-z0-9]+[_-])*api[_-]?key|apikey|appid|access[_-]?token|refresh[_-]?token|"
    r"service[_-]?auth[_-]?token|client[_-]?secret|password|passwd|secret|credential|"
    r"signature|token|key)\b\s*[:=]\s*[\"']?)(?P<value>[^\s&,;\"']+)",
    re.IGNORECASE,
)


def is_sensitive_name(name: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return (
        normalized in _SENSITIVE_NAMES
        or "apikey" in normalized
        or normalized.endswith("token")
        or "password" in normalized
        or "secret" in normalized
        or "credential" in normalized
        or "signature" in normalized
    )


def sensitive_values_from_mapping(values: Mapping[str, object] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(
        str(value)
        for key, value in values.items()
        if value is not None and is_sensitive_name(key) and str(value)
    )


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = urlencode(
            [
                (key, REDACTED if is_sensitive_name(key) else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
            safe="[]",
        )
        netloc = parts.netloc
        if parts.username is not None or parts.password is not None:
            host = parts.hostname or ""
            if parts.port is not None:
                host = f"{host}:{parts.port}"
            netloc = f"{REDACTED}@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "[INVALID URL REDACTED]"


def redact_text(text: object, secret_values: Iterable[str] = ()) -> str:
    result = str(text)
    result = _URL_RE.sub(lambda match: redact_url(match.group(0)), result)
    result = _AUTHORIZATION_RE.sub(f"Authorization: {REDACTED}", result)
    result = _BEARER_RE.sub(f"Bearer {REDACTED}", result)
    result = _LABELED_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", result
    )

    for secret in sorted({value for value in secret_values if len(value) >= 4}, key=len, reverse=True):
        for encoded in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            result = result.replace(encoded, REDACTED)
    return result


def redact_value(
    value: Any,
    secret_values: Iterable[str] = (),
    *,
    preserve_field_names: Iterable[str] = (),
) -> Any:
    secrets = tuple(secret_values)
    preserved = frozenset(str(name).casefold() for name in preserve_field_names)
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if is_sensitive_name(key) and str(key).casefold() not in preserved
                else redact_value(
                    item,
                    secrets,
                    preserve_field_names=preserved,
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_value(item, secrets, preserve_field_names=preserved)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_value(item, secrets, preserve_field_names=preserved)
            for item in value
        )
    return value


class RedactingFilter(logging.Filter):
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        super().__init__()
        self.secret_values = tuple(secret_values)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            if record.exc_info:
                rendered = "\n".join(
                    (rendered, "".join(traceback.format_exception(*record.exc_info)))
                )
            record.msg = redact_text(rendered, self.secret_values)
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        except Exception:
            record.msg = "Log message suppressed after redaction failure"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def install_redacting_log_filter(secret_values: Iterable[str] = ()) -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RedactingFilter(secret_values))
