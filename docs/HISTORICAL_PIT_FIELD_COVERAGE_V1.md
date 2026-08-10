# Historical PIT Nested Field Coverage V1

`DSE_MLB_HISTORICAL_PIT_FIELD_COVERAGE_V1` is a read-only capability inventory for the retained Retrosheet baseline. It describes which nested statistical fields are populated without emitting any historical field value or full `stats_json` payload.

The inventory reads only regular-season Retrosheet games within the requested inclusive date range. Player rows come from `stats_game_player_snapshots` and are grouped by season and the retained role `batting`, `pitching`, or `fielding`. Team rows come from `stats_game_team_snapshots` and are grouped by season and the exact `stats_json.stats_type` retained with the row. No field is mapped into `ModelFeatureSet V1` by this capability.

Each field-coverage row contains:

- `season`
- `table_name`
- `stats_type`
- `field_name`
- `total_applicable_row_count`, the complete retained row denominator for that season/table/stats-type group
- `non_null_row_count`
- `distinct_game_count`
- `distinct_player_count` for player rows, and `null` for team rows
- `observed_type` and the supporting sorted `observed_types` inventory

SQLite JSON types are reported as `integer`, `float`, `string`, `boolean`, `array`, `object`, or `null`. Nullability is represented separately: null plus one populated type retains the populated classification, while multiple populated types classify as `mixed`.

The canonical inventory binds the contract version, provider (`retrosheet`), requested date range, existing source-inventory checksum, and every deterministically ordered coverage row. SHA-256 of that canonical JSON is exposed as `field_coverage_checksum`.

## Safety and execution

The implementation reuses `open_historical_source_read_only()` and its raw SQLite `mode=ro` URI, `PRAGMA query_only=ON`, source containment rules, and symlink rejection. It does not construct `Database`, run migrations, write evidence, call a provider, or request network access. Applicable rows must contain valid JSON objects with a nested `values` object; team rows must also retain a nonblank textual `stats_type`.

Run the bounded inventory locally with:

```powershell
.\.venv\Scripts\python.exe scripts\inspect_historical_pit_source.py --database "<absolute-path-to-legacy-stats.sqlite3>" --start-date 2023-01-01 --end-date 2025-12-31 --field-coverage
```

The command writes one compact canonical JSON result to standard output. Redirect it to a new evidence file if desired; never point the output at the source database.
