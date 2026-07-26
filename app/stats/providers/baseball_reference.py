from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from types import MappingProxyType
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from app.stats.contracts import (
    CsvRow,
    RawArtifact,
    RequestParameter,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsResponse,
    StatsTransport,
)
from app.stats.providers._common import (
    read_exact_capture,
    reject_block_page,
    require_content_type,
)

BASEBALL_REFERENCE_BASE_URL = "https://www.baseball-reference.com"
BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS = 6.0
BASEBALL_REFERENCE_DAILY_PATH = "/leagues/daily.fcgi"
BASEBALL_REFERENCE_MAXIMUM_DAILY_RANGE_DAYS = 366
_TABLE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_KEY_RE = re.compile(r"[^a-z0-9]+")
_DAILY_PLAYER_IDENTITY_QUERY_RE = re.compile(
    r"player=1&mlb_ID=(?P<mlb_id>[1-9][0-9]{0,9})"
)
_BOX_SCORE_PATH_RE = re.compile(
    r"/boxes/[A-Za-z0-9]{3}/(?P<game_id>[A-Za-z0-9]{3}[0-9]{9})\.shtml"
)
_BOX_DATE_INDEX_QUERY_RE = re.compile(r"date=(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})")
_SCHEDULE_PREVIEW_ROW_KEYS = (
    "team_game",
    "date_game",
    "boxscore",
    "team_ID",
    "homeORvis",
    "opp_ID",
    "starttime",
    "preview",
    "day_or_night",
    "cli",
    "reschedule",
)
_SCHEDULE_PREVIEW_ROW_COLSPANS = (1, 1, 1, 1, 1, 1, 3, 10, 1, 1, 1)

_BATTING_DAILY_LABELS = (
    "Rk", "Name", "Age", "#days", "Lev", "Tm", "G", "PA", "AB", "R",
    "H", "2B", "3B", "HR", "RBI", "BB", "IBB", "SO", "HBP", "SH",
    "SF", "GDP", "SB", "CS", "BA", "OBP", "SLG", "OPS",
)
_PITCHING_DAILY_LABELS = (
    "Rk", "Name", "Age", "#days", "Lev", "Tm", "G", "GS", "W", "L",
    "SV", "IP", "H", "R", "ER", "BB", "SO", "HR", "HBP", "ERA",
    "AB", "2B", "3B", "IBB", "GDP", "SF", "SB", "CS", "PO", "BF",
    "Pit", "Str", "StL", "StS", "GB/FB", "LD", "PU", "WHIP", "BAbip",
    "SO9", "SO/W",
)

_BATTING_DAILY_LEGACY_COLUMNS = (
    "ranker", "name_display", "age", "number_of_days", "level", "team_name",
    "games", "plate_appearances", "at_bats", "runs", "hits", "doubles",
    "triples", "home_runs", "runs_batted_in", "bases_on_balls",
    "intentional_bases_on_balls", "strikeouts", "hit_by_pitch", "sacrifice_hits",
    "sacrifice_flies", "grounded_into_double_plays", "stolen_bases",
    "caught_stealing", "batting_avg", "onbase_perc", "slugging_perc",
    "onbase_plus_slugging",
)
_PITCHING_DAILY_LEGACY_COLUMNS = (
    "ranker", "name_display", "age", "number_of_days", "level", "team_name",
    "games", "games_started", "wins", "losses", "saves", "innings_pitched",
    "hits", "runs", "earned_runs", "bases_on_balls", "strikeouts", "home_runs",
    "hit_by_pitch", "earned_run_avg", "at_bats", "doubles", "triples",
    "intentional_bases_on_balls", "grounded_into_double_plays", "sacrifice_flies",
    "stolen_bases", "caught_stealing", "pickoffs", "batters_faced", "pitches",
    "strikes_total", "strikes_looking", "strikes_swinging", "grounded_fly_ratio",
    "line_drives", "popups", "whip", "batting_avg_balls_in_play",
    "strikeouts_per_nine", "strikeouts_per_walk",
)
_BATTING_DAILY_SOURCE_COLUMNS = (
    "ranker", "player", "gl", "age", "days_since_last_played", "level",
    "team_ID", "G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB",
    "IBB", "SO", "HBP", "SH", "SF", "GIDP", "SB", "CS", "batting_avg",
    "onbase_perc", "slugging_perc", "onbase_plus_slugging",
)
_PITCHING_DAILY_SOURCE_COLUMNS = (
    "ranker", "player", "gl", "age", "days_since_last_played", "level",
    "team_ID", "G", "GS", "W", "L", "S", "IP", "H", "R", "ER", "BB", "SO",
    "HR", "HBP", "earned_run_avg", "AB", "2B", "3B", "IBB", "GIDP", "SF",
    "SB", "CS", "pickoffs", "batters_faced", "pitches", "strikes_total",
    "strikes_looking", "strikes_swinging", "inplay_gb_total", "inplay_ld",
    "inplay_pu", "whip", "batting_avg_bip", "strikeouts_per_nine",
    "strikeouts_per_base_on_balls",
)
_COMMON_DAILY_SOURCE_COLUMN_MAP = {
    "player": "name_display",
    "gl": "game_log",
    "days_since_last_played": "number_of_days",
    "team_ID": "team_name",
    "G": "games",
    "H": "hits",
    "R": "runs",
    "2B": "doubles",
    "3B": "triples",
    "HR": "home_runs",
    "BB": "bases_on_balls",
    "IBB": "intentional_bases_on_balls",
    "SO": "strikeouts",
    "HBP": "hit_by_pitch",
    "SF": "sacrifice_flies",
    "GIDP": "grounded_into_double_plays",
    "SB": "stolen_bases",
    "CS": "caught_stealing",
    "AB": "at_bats",
}
_BATTING_DAILY_SOURCE_COLUMN_MAP = {
    **_COMMON_DAILY_SOURCE_COLUMN_MAP,
    "PA": "plate_appearances",
    "RBI": "runs_batted_in",
    "SH": "sacrifice_hits",
}
_PITCHING_DAILY_SOURCE_COLUMN_MAP = {
    **_COMMON_DAILY_SOURCE_COLUMN_MAP,
    "GS": "games_started",
    "W": "wins",
    "L": "losses",
    "S": "saves",
    "IP": "innings_pitched",
    "ER": "earned_runs",
    "inplay_gb_total": "grounded_fly_ratio",
    "inplay_ld": "line_drives",
    "inplay_pu": "popups",
    "batting_avg_bip": "batting_avg_balls_in_play",
    "strikeouts_per_base_on_balls": "strikeouts_per_walk",
}


def _commit_validated_cache(transport: StatsTransport, response: StatsResponse) -> None:
    """Reach through the acquisition recording decorator without coupling to it."""
    current: object | None = transport
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        commit = getattr(current, "commit_cache", None)
        if callable(commit):
            commit(response)
            return
        current = getattr(current, "transport", None)


def _column_key(value: str, index: int) -> str:
    normalized = _KEY_RE.sub("_", value.strip().casefold()).strip("_")
    return normalized or f"column_{index + 1}"


def _validate_daily_range(start_date: date, end_date: date) -> None:
    if (
        not isinstance(start_date, date)
        or isinstance(start_date, datetime)
        or not isinstance(end_date, date)
        or isinstance(end_date, datetime)
    ):
        raise TypeError("Baseball-Reference daily ranges require calendar dates")
    if start_date.year < 2008 or end_date.year < 2008:
        raise ValueError("Baseball-Reference daily ranges require year 2008 or later")
    if end_date < start_date:
        raise ValueError("Baseball-Reference daily range end must not precede start")
    if (end_date - start_date).days + 1 > BASEBALL_REFERENCE_MAXIMUM_DAILY_RANGE_DAYS:
        raise ValueError(
            "Baseball-Reference daily range exceeds the 366-day controlled bound"
        )


def _validated_relative_href(href: str) -> str:
    if not href.startswith("/") or href.startswith("//"):
        raise ValueError("source link is not origin-relative")
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("source link escapes the controlled origin")
    if unquote(parsed.path) != parsed.path or "\\" in parsed.path:
        raise ValueError("source link contains an encoded or alternate path")
    return href


def _daily_player_identity(links: tuple[str, ...]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    invalid_identity_link = False
    for href in links:
        parsed = urlsplit(href)
        if "mlb_ID" not in parse_qs(parsed.query, keep_blank_values=True):
            continue
        try:
            validated_href = _validated_relative_href(href)
        except ValueError:
            invalid_identity_link = True
            continue
        parsed = urlsplit(validated_href)
        match = _DAILY_PLAYER_IDENTITY_QUERY_RE.fullmatch(parsed.query)
        if parsed.path != "/redirect.fcgi" or match is None:
            invalid_identity_link = True
            continue
        candidates.append((match.group("mlb_id"), validated_href))
    identities = {candidate[0] for candidate in candidates}
    if invalid_identity_link or len(identities) != 1:
        raise ValueError("player row lacks one validated, unambiguous mlbID anchor")
    return candidates[0]


def _schedule_game_identity(links: tuple[str, ...]) -> tuple[str, str] | None:
    candidates: list[tuple[str, str]] = []
    invalid_box_score_link = False
    for href in links:
        if "/boxes/" not in unquote(href).casefold():
            continue
        try:
            validated_href = _validated_relative_href(href)
        except ValueError:
            invalid_box_score_link = True
            continue
        parsed = urlsplit(validated_href)
        if parsed.path == "/boxes/":
            match = _BOX_DATE_INDEX_QUERY_RE.fullmatch(parsed.query)
            if match is None:
                invalid_box_score_link = True
                continue
            observed_date = match.group("date")
            try:
                parsed_date = date.fromisoformat(observed_date)
            except ValueError:
                invalid_box_score_link = True
                continue
            if parsed_date.isoformat() != observed_date:
                invalid_box_score_link = True
                continue
            continue
        if parsed.query:
            invalid_box_score_link = True
            continue
        match = _BOX_SCORE_PATH_RE.fullmatch(parsed.path)
        if match is None:
            invalid_box_score_link = True
            continue
        candidates.append((match.group("game_id"), validated_href))
    identities = {candidate[0] for candidate in candidates}
    if invalid_box_score_link or len(identities) > 1:
        raise ValueError("schedule row contains an invalid or ambiguous boxscore anchor")
    return candidates[0] if candidates else None


class _TableParser(HTMLParser):
    def __init__(self, table_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.table_depth = 0
        self.found_table = False
        self.comments: list[str] = []
        self.columns: list[str] = []
        self.column_labels: list[str] = []
        self.rows: list[CsvRow] = []
        self.row_links: list[tuple[str, ...]] = []
        self.invalid_reason: str | None = None
        self._row: (
            list[tuple[str | None, str, bool, tuple[str, ...], str | None]]
            | None
        ) = None
        self._cell_key: str | None = None
        self._cell_header = False
        self._cell_text: list[str] | None = None
        self._cell_links: list[str] | None = None
        self._cell_colspan: str | None = None

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "table":
            if self.table_depth:
                self.table_depth += 1
            elif self.table_id is None or attributes.get("id") == self.table_id:
                self.table_depth = 1
                self.found_table = True
            return
        if not self.table_depth:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_key = attributes.get("data-stat")
            self._cell_header = tag == "th"
            self._cell_text = []
            self._cell_links = []
            self._cell_colspan = attributes.get("colspan")
        elif tag == "a" and self._cell_text is not None:
            href = attributes.get("href")
            if href and self._cell_links is not None:
                self._cell_links.append(href)

    def handle_data(self, data: str) -> None:
        if self.table_depth and self._cell_text is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.table_depth:
            return
        if tag in {"th", "td"} and self._cell_text is not None and self._row is not None:
            self._row.append(
                (
                    self._cell_key,
                    " ".join("".join(self._cell_text).split()),
                    self._cell_header,
                    tuple(self._cell_links or ()),
                    self._cell_colspan,
                )
            )
            self._cell_key = None
            self._cell_header = False
            self._cell_text = None
            self._cell_links = None
            self._cell_colspan = None
        elif tag == "tr" and self._row is not None:
            self._finish_row(self._row)
            self._row = None
        elif tag == "table":
            self.table_depth -= 1

    def _finish_row(
        self,
        cells: list[
            tuple[str | None, str, bool, tuple[str, ...], str | None]
        ],
    ) -> None:
        if not cells:
            return
        if all(is_header for _, _, is_header, _, _ in cells):
            if not self.columns:
                candidate_columns = [
                    key.strip() if key and key.strip() else _column_key(text, index)
                    for index, (key, text, _, _, _) in enumerate(cells)
                ]
                if len(candidate_columns) != len(set(candidate_columns)):
                    self.invalid_reason = "duplicate header columns"
                else:
                    self.columns = candidate_columns
                    self.column_labels = [text for _, text, _, _, _ in cells]
            return
        keys = [
            key.strip()
            if key and key.strip()
            else (
                self.columns[index]
                if index < len(self.columns)
                else f"column_{index + 1}"
            )
            for index, (key, _, _, _, _) in enumerate(cells)
        ]
        if len(keys) != len(set(keys)):
            self.invalid_reason = "duplicate row columns"
            return
        if not self.columns:
            self.columns = keys
        elif len(keys) != len(self.columns):
            compact_error = self._schedule_preview_row_error(cells)
            if compact_error is not None:
                self.invalid_reason = compact_error
                return
        row = {keys[index]: cell[1] for index, cell in enumerate(cells)}
        self.rows.append(row)
        self.row_links.append(
            tuple(link for cell in cells for link in cell[3])
        )

    def _schedule_preview_row_error(
        self,
        cells: list[
            tuple[str | None, str, bool, tuple[str, ...], str | None]
        ],
    ) -> str | None:
        if self.table_id != "team_schedule":
            return "row width does not match the table header"
        explicit_keys = tuple(
            key.strip() if key and key.strip() else "" for key, *_ in cells
        )
        if explicit_keys != _SCHEDULE_PREVIEW_ROW_KEYS:
            return "unrecognized compact team_schedule row"

        colspans: list[int] = []
        for cell in cells:
            raw_colspan = cell[4]
            if raw_colspan is None:
                colspans.append(1)
                continue
            if not raw_colspan.isascii() or not raw_colspan.isdecimal():
                return "compact team_schedule row has an invalid colspan"
            colspan = int(raw_colspan)
            if colspan < 1:
                return "compact team_schedule row has an invalid colspan"
            colspans.append(colspan)
        if tuple(colspans) != _SCHEDULE_PREVIEW_ROW_COLSPANS:
            return "compact team_schedule row has unexpected colspans"
        if sum(colspans) != len(self.columns):
            return "compact team_schedule row does not span the table header"

        values = {key: cells[index][1] for index, key in enumerate(explicit_keys)}
        for key in ("team_game", "date_game", "boxscore", "team_ID", "opp_ID"):
            if not values[key]:
                return f"compact team_schedule row lacks required {key}"
        if values["boxscore"].casefold() != "preview" or not values["preview"]:
            return "compact team_schedule row lacks explicit preview semantics"
        if values["homeORvis"] not in {"", "@"}:
            return "compact team_schedule row has ambiguous home/away identity"
        if values["team_ID"] == values["opp_ID"]:
            return "compact team_schedule row has ambiguous team identity"
        return None


@dataclass(frozen=True, slots=True)
class BaseballReferenceTable:
    table_id: str
    columns: tuple[str, ...]
    column_labels: tuple[str, ...]
    rows: tuple[CsvRow, ...]
    raw: RawArtifact
    response_headers: Mapping[str, str]
    attempts: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "response_headers", MappingProxyType(dict(self.response_headers))
        )


class BaseballReferenceProvider:
    def __init__(
        self,
        transport: StatsTransport,
        *,
        base_url: str = BASEBALL_REFERENCE_BASE_URL,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("Baseball-Reference base URL must be an HTTPS origin")
        self.transport = transport
        self.base_url = base_url.rstrip("/") + "/"

    def collect_batting_stats_range(
        self,
        start_date: date,
        end_date: date,
        *,
        fixture_key: str | None = None,
    ) -> BaseballReferenceTable:
        return self._collect_daily_stats_range(
            start_date,
            end_date,
            statistics_type="b",
            expected_labels=_BATTING_DAILY_LABELS,
            fixture_key=fixture_key,
        )

    def collect_pitching_stats_range(
        self,
        start_date: date,
        end_date: date,
        *,
        fixture_key: str | None = None,
    ) -> BaseballReferenceTable:
        return self._collect_daily_stats_range(
            start_date,
            end_date,
            statistics_type="p",
            expected_labels=_PITCHING_DAILY_LABELS,
            fixture_key=fixture_key,
        )

    def _collect_daily_stats_range(
        self,
        start_date: date,
        end_date: date,
        *,
        statistics_type: str,
        expected_labels: tuple[str, ...],
        fixture_key: str | None,
    ) -> BaseballReferenceTable:
        _validate_daily_range(start_date, end_date)
        name = "batting" if statistics_type == "b" else "pitching"
        table = self.collect_table(
            BASEBALL_REFERENCE_DAILY_PATH,
            table_id="daily",
            params={
                "user_team": "",
                "bust_cache": "",
                "type": statistics_type,
                "lastndays": "7",
                "dates": "fromandto",
                "fromandto": f"{start_date.isoformat()}.{end_date.isoformat()}",
                "level": "mlb",
                "franch": "",
                "stat": "",
                "stat_value": "0",
            },
            fixture_key=fixture_key
            or (
                "baseball_reference:daily_"
                f"{name}:{start_date.isoformat()}:{end_date.isoformat()}"
            ),
        )
        observed_labels = tuple(label for label in table.column_labels if label)
        if observed_labels != expected_labels:
            raise StatsProviderPayloadError(
                f"Baseball-Reference daily {name} schema did not match the "
                "pybaseball 2.2.7 contract",
                capture=table.raw,
            )
        if not table.rows:
            raise StatsProviderPayloadError(
                f"Baseball-Reference daily {name} table was empty",
                capture=table.raw,
            )
        return self._normalize_daily_source_columns(
            table,
            statistics_type=statistics_type,
        )

    @staticmethod
    def _normalize_daily_source_columns(
        table: BaseballReferenceTable,
        *,
        statistics_type: str,
    ) -> BaseballReferenceTable:
        if len(table.columns) < 2 or table.columns[-2:] != (
            "mlbID",
            "mlbID_href",
        ):
            raise StatsProviderPayloadError(
                "Baseball-Reference daily identity columns are missing",
                capture=table.raw,
            )
        source_columns = table.columns[:-2]
        legacy_columns: tuple[str, ...]
        provider_columns: tuple[str, ...]
        column_map: Mapping[str, str]
        if statistics_type == "b":
            legacy_columns = _BATTING_DAILY_LEGACY_COLUMNS
            provider_columns = _BATTING_DAILY_SOURCE_COLUMNS
            column_map = _BATTING_DAILY_SOURCE_COLUMN_MAP
        elif statistics_type == "p":
            legacy_columns = _PITCHING_DAILY_LEGACY_COLUMNS
            provider_columns = _PITCHING_DAILY_SOURCE_COLUMNS
            column_map = _PITCHING_DAILY_SOURCE_COLUMN_MAP
        else:
            raise StatsProviderPayloadError(
                "Baseball-Reference daily statistics type is unsupported",
                capture=table.raw,
            )
        if source_columns == legacy_columns:
            return table
        if source_columns != provider_columns:
            raise StatsProviderPayloadError(
                "Baseball-Reference daily data-stat schema is unrecognized",
                capture=table.raw,
            )

        normalized_columns = tuple(
            column_map.get(column, column) for column in table.columns
        )
        if len(normalized_columns) != len(set(normalized_columns)):
            raise StatsProviderPayloadError(
                "Baseball-Reference daily data-stat mapping collided",
                capture=table.raw,
            )
        normalized_rows = tuple(
            MappingProxyType(
                {column_map.get(key, key): value for key, value in row.items()}
            )
            for row in table.rows
        )
        if any(len(row) != len(table.rows[index]) for index, row in enumerate(normalized_rows)):
            raise StatsProviderPayloadError(
                "Baseball-Reference daily row mapping collided",
                capture=table.raw,
            )
        return BaseballReferenceTable(
            table_id=table.table_id,
            columns=normalized_columns,
            column_labels=table.column_labels,
            rows=normalized_rows,
            raw=table.raw,
            response_headers=table.response_headers,
            attempts=table.attempts,
        )

    def collect_table(
        self,
        path: str,
        *,
        table_id: str | None,
        params: Mapping[str, RequestParameter] | None = None,
        fixture_key: str | None = None,
        persistent_cache: bool = True,
    ) -> BaseballReferenceTable:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Baseball-Reference path must be an origin-relative path")
        parsed_path = urlsplit(path)
        if parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment:
            raise ValueError("Baseball-Reference query values must be supplied separately")
        if table_id is not None and _TABLE_ID_RE.fullmatch(table_id) is None:
            raise ValueError("Baseball-Reference table_id is invalid")
        url = urljoin(self.base_url, path.lstrip("/"))
        response = self.transport.fetch(
            StatsRequest(
                provider=StatsProvider.BASEBALL_REFERENCE,
                endpoint_category="html_table",
                fixture_key=fixture_key or f"baseball_reference:{path}:{table_id}",
                url=url,
                params=params or {},
                headers={
                    "Accept": "text/html, application/xhtml+xml;q=0.9",
                    "Accept-Language": "en-US,en;q=0.8",
                },
                timeout_seconds=30.0,
                max_attempts=3,
                minimum_interval_seconds=BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS,
                persistent_cache=persistent_cache,
            )
        )
        payload = read_exact_capture(response.capture)
        require_content_type(
            response.capture,
            {"text/html", "application/xhtml+xml"},
        )
        reject_block_page(payload, response.capture)
        try:
            html = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StatsProviderPayloadError(
                "Baseball-Reference response is not valid UTF-8 HTML",
                capture=response.capture,
            ) from exc
        if "<html" not in html[:20_000].casefold():
            raise StatsProviderPayloadError(
                "Baseball-Reference response is not an HTML document",
                capture=response.capture,
            )

        parser = _TableParser(table_id)
        parser.feed(html)
        selected = parser
        if not parser.found_table:
            for comment in parser.comments:
                if "<table" not in comment.casefold():
                    continue
                candidate = _TableParser(table_id)
                candidate.feed(comment)
                if candidate.found_table:
                    selected = candidate
                    break
        if not selected.found_table:
            raise StatsProviderPayloadError(
                f"Baseball-Reference table {(table_id or 'first_table')!r} was not present",
                capture=response.capture,
            )
        if selected.invalid_reason is not None:
            raise StatsProviderPayloadError(
                f"Baseball-Reference table {table_id!r} is malformed: "
                f"{selected.invalid_reason}",
                capture=response.capture,
            )
        if not selected.columns:
            raise StatsProviderPayloadError(
                f"Baseball-Reference table {table_id!r} had no columns",
                capture=response.capture,
            )
        columns = list(selected.columns)
        rows = [dict(row) for row in selected.rows]
        if table_id == "daily":
            if {"mlbID", "mlbID_href"}.intersection(columns):
                raise StatsProviderPayloadError(
                    "Baseball-Reference daily table collided with identity columns",
                    capture=response.capture,
                )
            for row, links in zip(rows, selected.row_links, strict=True):
                try:
                    mlb_id, href = _daily_player_identity(links)
                except ValueError as exc:
                    raise StatsProviderPayloadError(
                        f"Baseball-Reference daily table identity is malformed: {exc}",
                        capture=response.capture,
                    ) from exc
                row["mlbID"] = mlb_id
                row["mlbID_href"] = href
            columns.extend(("mlbID", "mlbID_href"))
        elif table_id == "team_schedule":
            if {"game_id", "boxscore_href"}.intersection(columns):
                raise StatsProviderPayloadError(
                    "Baseball-Reference schedule collided with identity columns",
                    capture=response.capture,
                )
            for row, links in zip(rows, selected.row_links, strict=True):
                try:
                    identity = _schedule_game_identity(links)
                except ValueError as exc:
                    raise StatsProviderPayloadError(
                        f"Baseball-Reference schedule identity is malformed: {exc}",
                        capture=response.capture,
                    ) from exc
                if identity is not None:
                    row["game_id"], row["boxscore_href"] = identity
            columns.extend(("game_id", "boxscore_href"))
        table = BaseballReferenceTable(
            table_id=table_id or "first_table",
            columns=tuple(columns),
            column_labels=tuple(selected.column_labels),
            rows=tuple(MappingProxyType(row) for row in rows),
            raw=response.capture,
            response_headers=response.headers,
            attempts=response.attempts,
        )
        _commit_validated_cache(self.transport, response)
        return table
