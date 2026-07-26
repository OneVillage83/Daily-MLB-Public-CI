from __future__ import annotations

import importlib
import importlib.metadata
import math
import re
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType, ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PYBASEBALL_REQUIRED_VERSION = "2.2.7"
_TEAM_ID_RE = re.compile(r"[A-Z]{2,3}")
_IDENTITY_FIELDS = frozenset(
    {"key_mlbam", "key_retro", "key_bbref", "key_fangraphs"}
)
_PYBASEBALL_IDENTITY_KEYS = {
    "key_mlbam": "mlbam",
    "key_retro": "retro",
    "key_bbref": "bbref",
    "key_fangraphs": "fangraphs",
}
_BASEBALL_REFERENCE_HOSTS = frozenset(
    {"baseball-reference.com", "www.baseball-reference.com"}
)
_LEGACY_BREF_SESSION_MODULES = (
    "pybaseball.league_batting_stats",
    "pybaseball.league_pitching_stats",
    "pybaseball.team_results",
)


class PybaseballParityError(RuntimeError):
    pass


class PybaseballVersionError(PybaseballParityError):
    pass


class PybaseballIdentityError(PybaseballParityError):
    pass


@dataclass(frozen=True, slots=True)
class PybaseballFunctions:
    batting_stats_range: Callable[..., Any]
    pitching_stats_range: Callable[..., Any]
    schedule_and_record: Callable[..., Any]
    statcast: Callable[..., Any]
    playerid_lookup: Callable[..., Any]
    playerid_reverse_lookup: Callable[..., Any]

    @classmethod
    def from_module(cls, module: ModuleType) -> PybaseballFunctions:
        functions: dict[str, Callable[..., Any]] = {}
        for name in cls.__dataclass_fields__:
            value = getattr(module, name, None)
            if not callable(value):
                raise PybaseballParityError(
                    f"pybaseball {PYBASEBALL_REQUIRED_VERSION} does not expose {name}"
                )
            functions[name] = value
        return cls(**functions)


@dataclass(frozen=True, slots=True)
class HttpsUrlPatch:
    owner: object
    attribute: str
    https_value: str

    def __post_init__(self) -> None:
        attribute = self.attribute.strip()
        parsed = urlsplit(self.https_value)
        if (
            not attribute
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("pybaseball URL patches require an explicit HTTPS URL")
        object.__setattr__(self, "attribute", attribute)

    def apply(self) -> _AppliedHttpsPatch:
        return _AppliedHttpsPatch(self)


class _AppliedHttpsPatch:
    def __init__(self, patch: HttpsUrlPatch) -> None:
        self.patch = patch
        self.original: str | None = None

    def __enter__(self) -> None:
        current = getattr(self.patch.owner, self.patch.attribute, None)
        if not isinstance(current, str):
            raise PybaseballParityError(
                f"pybaseball URL patch target {self.patch.attribute!r} is not text"
            )
        current_url = urlsplit(current)
        replacement_url = urlsplit(self.patch.https_value)
        if current_url.scheme not in {"http", "https"} or not current_url.hostname:
            raise PybaseballParityError(
                f"pybaseball URL patch target {self.patch.attribute!r} is not an HTTP URL"
            )
        if (
            current_url.netloc.casefold() != replacement_url.netloc.casefold()
            or current_url.path != replacement_url.path
            or current_url.query != replacement_url.query
            or current_url.fragment != replacement_url.fragment
        ):
            raise PybaseballParityError(
                "pybaseball URL patches may upgrade HTTPS only; destination changes are forbidden"
            )
        self.original = current
        setattr(self.patch.owner, self.patch.attribute, self.patch.https_value)

    def __exit__(self, *exc_info: object) -> None:
        if self.original is not None:
            setattr(self.patch.owner, self.patch.attribute, self.original)


@dataclass(frozen=True, slots=True)
class HttpsSessionPatch:
    """Temporarily upgrades legacy pybaseball Baseball-Reference requests."""

    owner: object
    attribute: str = "session"

    def __post_init__(self) -> None:
        attribute = self.attribute.strip()
        if not attribute:
            raise ValueError("pybaseball session patch requires an attribute")
        object.__setattr__(self, "attribute", attribute)

    def apply(self) -> _AppliedHttpsSessionPatch:
        return _AppliedHttpsSessionPatch(self)


class _HttpsSessionProxy:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(url, str):
            raise PybaseballParityError("pybaseball request URL must be text")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PybaseballParityError(
                "pybaseball Baseball-Reference request must use an HTTP(S) URL"
            )
        if parsed.hostname.casefold() not in _BASEBALL_REFERENCE_HOSTS:
            raise PybaseballParityError(
                "pybaseball Baseball-Reference session attempted an unexpected host"
            )
        upgraded = urlunsplit(
            (
                "https",
                parsed.netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        getter = getattr(self.delegate, "get", None)
        if not callable(getter):
            raise PybaseballParityError(
                "pybaseball Baseball-Reference session does not expose get"
            )
        return getter(upgraded, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


class _AppliedHttpsSessionPatch:
    def __init__(self, patch: HttpsSessionPatch) -> None:
        self.patch = patch
        self.original: object | None = None

    def __enter__(self) -> None:
        current = getattr(self.patch.owner, self.patch.attribute, None)
        if current is None or not callable(getattr(current, "get", None)):
            raise PybaseballParityError(
                f"pybaseball session patch target {self.patch.attribute!r} is invalid"
            )
        self.original = current
        setattr(
            self.patch.owner,
            self.patch.attribute,
            _HttpsSessionProxy(current),
        )

    def __exit__(self, *exc_info: object) -> None:
        if self.original is not None:
            setattr(self.patch.owner, self.patch.attribute, self.original)


@dataclass(frozen=True, slots=True)
class PybaseballProvenance:
    package: str = "pybaseball"
    version: str = PYBASEBALL_REQUIRED_VERSION
    role: str = "noncanonical_parity_reference"
    canonical: bool = False


@dataclass(frozen=True, slots=True)
class PybaseballParityResult:
    operation: str
    records: tuple[Mapping[str, Any], ...]
    parameters: Mapping[str, Any]
    provenance: PybaseballProvenance = field(default_factory=PybaseballProvenance)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            tuple(MappingProxyType(dict(record)) for record in self.records),
        )
        object.__setattr__(
            self, "parameters", MappingProxyType(dict(self.parameters))
        )


def _records(value: object, operation: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows = value
    else:
        converter = getattr(value, "to_dict", None)
        if not callable(converter):
            raise PybaseballParityError(
                f"pybaseball {operation} returned an unsupported result type"
            )
        try:
            rows = converter(orient="records")
        except TypeError:
            rows = converter("records")
    if not isinstance(rows, Sequence):
        raise PybaseballParityError(
            f"pybaseball {operation} did not produce record-oriented rows"
        )
    normalized: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PybaseballParityError(
                f"pybaseball {operation} row {index} is not a mapping"
            )
        normalized.append(MappingProxyType(dict(row)))
    return tuple(normalized)


def _date_value(value: date | str, field_name: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{field_name} must be an explicit ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an explicit ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be an explicit ISO date")
    return value


def _date_range(start: date | str, end: date | str) -> tuple[str, str]:
    start_value = _date_value(start, "start_date")
    end_value = _date_value(end, "end_date")
    if end_value < start_value:
        raise ValueError("end_date must not precede start_date")
    return start_value, end_value


def _team_id(value: str) -> str:
    if not isinstance(value, str) or _TEAM_ID_RE.fullmatch(value) is None:
        raise ValueError("team must be an exact uppercase provider team ID")
    return value


def _identity_token(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise PybaseballIdentityError("player identity IDs must be explicit values")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise PybaseballIdentityError("numeric player identity IDs must be integers")
        return str(int(value))
    normalized = str(value).strip()
    if not normalized or normalized.casefold() in {"na", "n/a", "nan", "none", "null"}:
        raise PybaseballIdentityError("player identity IDs must not be blank")
    return normalized


def _discover_https_upgrades() -> tuple[HttpsUrlPatch, ...]:
    patches: list[HttpsUrlPatch] = []
    for name, module in tuple(sys.modules.items()):
        if not (name == "pybaseball" or name.startswith("pybaseball.")):
            continue
        if not isinstance(module, ModuleType):
            continue
        for attribute, value in vars(module).items():
            lowered = attribute.casefold()
            if not isinstance(value, str) or not value.startswith("http://"):
                continue
            if not any(token in lowered for token in ("url", "uri", "endpoint")):
                continue
            patches.append(
                HttpsUrlPatch(module, attribute, "https://" + value.removeprefix("http://"))
            )
    return tuple(patches)


def _discover_https_session_upgrades() -> tuple[HttpsSessionPatch, ...]:
    patches: list[HttpsSessionPatch] = []
    for module_name in _LEGACY_BREF_SESSION_MODULES:
        module = sys.modules.get(module_name)
        if isinstance(module, ModuleType) and callable(
            getattr(getattr(module, "session", None), "get", None)
        ):
            patches.append(HttpsSessionPatch(module))
    return tuple(patches)


class PybaseballParityAdapter:
    """Pinned, explicitly noncanonical reference wrapper around pybaseball."""

    def __init__(
        self,
        functions: PybaseballFunctions,
        *,
        installed_version: str,
        https_patches: Iterable[HttpsUrlPatch] = (),
        https_session_patches: Iterable[HttpsSessionPatch] = (),
    ) -> None:
        if installed_version != PYBASEBALL_REQUIRED_VERSION:
            raise PybaseballVersionError(
                f"pybaseball {PYBASEBALL_REQUIRED_VERSION} is required; "
                f"found {installed_version!r}"
            )
        self.functions = functions
        self.provenance = PybaseballProvenance(version=installed_version)
        self.https_patches = tuple(https_patches)
        self.https_session_patches = tuple(https_session_patches)
        self._serial_lock = threading.RLock()

    @classmethod
    def from_installed(cls) -> PybaseballParityAdapter:
        try:
            version = importlib.metadata.version("pybaseball")
        except importlib.metadata.PackageNotFoundError as exc:
            raise PybaseballVersionError(
                "The optional pybaseball 2.2.7 parity profile is not installed"
            ) from exc
        if version != PYBASEBALL_REQUIRED_VERSION:
            raise PybaseballVersionError(
                f"pybaseball {PYBASEBALL_REQUIRED_VERSION} is required; found {version!r}"
            )
        module = importlib.import_module("pybaseball")
        functions = PybaseballFunctions.from_module(module)
        return cls(
            functions,
            installed_version=version,
            https_patches=_discover_https_upgrades(),
            https_session_patches=_discover_https_session_upgrades(),
        )

    def batting_stats_range(
        self, start_date: date | str, end_date: date | str
    ) -> PybaseballParityResult:
        start, end = _date_range(start_date, end_date)
        return self._call(
            "batting_stats_range",
            self.functions.batting_stats_range,
            (start, end),
            {},
            {"start_date": start, "end_date": end},
        )

    def pitching_stats_range(
        self, start_date: date | str, end_date: date | str
    ) -> PybaseballParityResult:
        start, end = _date_range(start_date, end_date)
        return self._call(
            "pitching_stats_range",
            self.functions.pitching_stats_range,
            (start, end),
            {},
            {"start_date": start, "end_date": end},
        )

    def schedule_and_record(
        self, season: int, team: str
    ) -> PybaseballParityResult:
        if isinstance(season, bool) or not isinstance(season, int) or not 1871 <= season <= 9999:
            raise ValueError("season must be a four-digit major-league season")
        exact_team = _team_id(team)
        return self._call(
            "schedule_and_record",
            self.functions.schedule_and_record,
            (season, exact_team),
            {},
            {"season": season, "team": exact_team},
        )

    def statcast(
        self,
        start_date: date | str,
        end_date: date | str,
        *,
        team: str | None = None,
    ) -> PybaseballParityResult:
        start, end = _date_range(start_date, end_date)
        exact_team = _team_id(team) if team is not None else None
        return self._call(
            "statcast",
            self.functions.statcast,
            (start, end),
            {"team": exact_team, "verbose": False, "parallel": False},
            {
                "start_date": start,
                "end_date": end,
                "team": exact_team,
                "parallel": False,
            },
        )

    def playerid_lookup(
        self,
        last_name: str,
        first_name: str,
        *,
        expected_id_field: str | None = None,
        expected_id: object = None,
    ) -> PybaseballParityResult:
        last = self._exact_name(last_name, "last_name")
        first = self._exact_name(first_name, "first_name")
        if (expected_id_field is None) != (expected_id is None):
            raise ValueError("expected_id_field and expected_id must be provided together")
        if expected_id_field is not None and expected_id_field not in _IDENTITY_FIELDS:
            raise ValueError("expected_id_field is not a supported exact identity key")
        result = self._call(
            "playerid_lookup",
            self.functions.playerid_lookup,
            (last, first),
            {"fuzzy": False},
            {
                "last_name": last,
                "first_name": first,
                "fuzzy": False,
                "expected_id_field": expected_id_field,
                "expected_id": expected_id,
            },
        )
        exact_rows = tuple(
            row
            for row in result.records
            if str(row.get("name_last", "")).strip().casefold() == last.casefold()
            and str(row.get("name_first", "")).strip().casefold() == first.casefold()
        )
        if expected_id_field is not None:
            expected_token = _identity_token(expected_id)
            exact_rows = tuple(
                row
                for row in exact_rows
                if _identity_token(row.get(expected_id_field)) == expected_token
            )
        if len(exact_rows) != 1:
            raise PybaseballIdentityError(
                "exact player lookup must resolve to one identity row; fuzzy merging is forbidden"
            )
        stable_identity = False
        for identity_field in _IDENTITY_FIELDS:
            if identity_field not in exact_rows[0]:
                continue
            try:
                _identity_token(exact_rows[0][identity_field])
            except PybaseballIdentityError:
                continue
            stable_identity = True
            break
        if not stable_identity:
            raise PybaseballIdentityError(
                "exact player lookup returned no stable provider identity ID"
            )
        return PybaseballParityResult(
            operation=result.operation,
            records=exact_rows,
            parameters=result.parameters,
            provenance=self.provenance,
        )

    def playerid_reverse_lookup(
        self,
        player_ids: Sequence[object],
        *,
        key_type: str = "key_mlbam",
    ) -> PybaseballParityResult:
        if key_type not in _IDENTITY_FIELDS:
            raise ValueError("key_type is not a supported exact identity key")
        tokens = tuple(_identity_token(value) for value in player_ids)
        if not tokens or len(tokens) != len(set(tokens)):
            raise PybaseballIdentityError(
                "reverse lookup requires non-empty unique exact identity IDs"
            )
        result = self._call(
            "playerid_reverse_lookup",
            self.functions.playerid_reverse_lookup,
            (list(player_ids),),
            {"key_type": _PYBASEBALL_IDENTITY_KEYS[key_type]},
            {"player_ids": tokens, "key_type": key_type},
        )
        returned: dict[str, Mapping[str, Any]] = {}
        for row in result.records:
            token = _identity_token(row.get(key_type))
            if token in returned:
                raise PybaseballIdentityError(
                    "reverse lookup returned duplicate rows for one identity ID"
                )
            returned[token] = row
        if set(returned) != set(tokens):
            raise PybaseballIdentityError(
                "reverse lookup did not return exactly one row for every requested identity ID"
            )
        ordered = tuple(returned[token] for token in tokens)
        return PybaseballParityResult(
            operation=result.operation,
            records=ordered,
            parameters=result.parameters,
            provenance=self.provenance,
        )

    @staticmethod
    def _exact_name(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field_name} must be a non-blank exact name")
        return value

    def _call(
        self,
        operation: str,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        parameters: Mapping[str, Any],
    ) -> PybaseballParityResult:
        with self._serial_lock, ExitStack() as stack:
            for url_patch in self.https_patches:
                stack.enter_context(url_patch.apply())
            for session_patch in self.https_session_patches:
                stack.enter_context(session_patch.apply())
            value = function(*args, **dict(kwargs))
        return PybaseballParityResult(
            operation=operation,
            records=_records(value, operation),
            parameters=parameters,
            provenance=self.provenance,
        )
