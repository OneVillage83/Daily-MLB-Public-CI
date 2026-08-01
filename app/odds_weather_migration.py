from __future__ import annotations


_DATE_CHECK = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
_SHA_CHECK = "length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*'"
_AWARE_CHECK = (
    "length(trim({name}))>0 AND "
    "(substr({name},-1)='Z' OR substr({name},-6,1) IN ('+','-'))"
)


def _sha(name: str) -> str:
    return _SHA_CHECK.format(name=name)


def _aware(name: str) -> str:
    return _AWARE_CHECK.format(name=name)


ODDS_WEATHER_SCHEMA_V11_TABLE_STATEMENTS = (
    f"""
    CREATE TABLE odds_weather_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'odds_weather'
            CHECK (phase_key='odds_weather'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE_CHECK}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        outcome TEXT NOT NULL CHECK (
            outcome IN ('assembled','acquisition_failed','normalization_failed','assembly_failed')
        ),
        snapshot_checksum TEXT CHECK (
            snapshot_checksum IS NULL OR ({_sha('snapshot_checksum')})
        ),
        evidence_manifest_relpath TEXT NOT NULL CHECK (length(trim(evidence_manifest_relpath))>0),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha('evidence_manifest_checksum')}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>=0),
        warnings_json TEXT NOT NULL CHECK (
            json_valid(warnings_json) AND json_type(warnings_json)='array'
        ),
        warning_count INTEGER NOT NULL CHECK (
            warning_count>=0 AND warning_count=json_array_length(warnings_json)
        ),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        completed_at TEXT NOT NULL CHECK ({_aware('completed_at')}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_key)
            REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_daily_slate_snapshot_id)
            REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_game_state_snapshot_id)
            REFERENCES game_state_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_baseball_intelligence_snapshot_id)
            REFERENCES baseball_intelligence_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (
            (outcome='assembled' AND snapshot_checksum IS NOT NULL)
            OR (outcome<>'assembled' AND snapshot_checksum IS NULL)
        )
    )
    """,
    f"""
    CREATE TABLE odds_weather_snapshots (
        snapshot_id TEXT NOT NULL PRIMARY KEY CHECK (
            snapshot_id='odds-weather:' || snapshot_checksum
        ),
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'odds_weather' CHECK (phase_key='odds_weather'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE_CHECK}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        sport TEXT NOT NULL CHECK (sport='MLB'),
        league TEXT NOT NULL CHECK (league='MLB'),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_ODDS_WEATHER_V1'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha('snapshot_checksum')}),
        source_raw_capture_checksums_json TEXT NOT NULL CHECK (
            json_valid(source_raw_capture_checksums_json)
            AND json_type(source_raw_capture_checksums_json)='array'
        ),
        warnings_json TEXT NOT NULL CHECK (
            json_valid(warnings_json) AND json_type(warnings_json)='array'
        ),
        warning_count INTEGER NOT NULL CHECK (
            warning_count>=0 AND warning_count=json_array_length(warnings_json)
        ),
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json)
            AND json_type(canonical_json,'$.checksum')='text'
            AND json_extract(canonical_json,'$.checksum')=snapshot_checksum
        ),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        selected_raw_capture_count INTEGER NOT NULL CHECK (selected_raw_capture_count>=0),
        weather_selection_count INTEGER NOT NULL CHECK (weather_selection_count>=0),
        artifact_relpath TEXT NOT NULL CHECK (
            artifact_relpath=
                'odds_weather/snapshots/' || snapshot_checksum ||
                '/odds_weather_v1.json'
        ),
        artifact_checksum TEXT NOT NULL CHECK ({_sha('artifact_checksum')}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware('sealed_at')})),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_key)
            REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_attempt)
            REFERENCES odds_weather_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_daily_slate_snapshot_id)
            REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_game_state_snapshot_id)
            REFERENCES game_state_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_baseball_intelligence_snapshot_id)
            REFERENCES baseball_intelligence_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_raw_captures (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        provider TEXT NOT NULL CHECK (provider IN ('the_odds_api','nws','openweather')),
        endpoint_category TEXT NOT NULL CHECK (
            endpoint_category IN ('mlb_odds','point_lookup','hourly_forecast','one_call')
        ),
        source_game_id TEXT,
        provider_event_id TEXT,
        retrieved_at TEXT NOT NULL CHECK ({_aware('retrieved_at')}),
        provider_timestamp TEXT CHECK (
            provider_timestamp IS NULL OR ({_aware('provider_timestamp')})
        ),
        raw_relpath TEXT NOT NULL CHECK (length(trim(raw_relpath))>0),
        raw_capture_checksum TEXT NOT NULL CHECK ({_sha('raw_capture_checksum')}),
        raw_byte_count INTEGER NOT NULL CHECK (raw_byte_count>=0),
        canonical_metadata_json TEXT NOT NULL CHECK (
            json_valid(canonical_metadata_json) AND json_type(canonical_metadata_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(run_id,phase_attempt,raw_capture_checksum),
        UNIQUE(run_id,phase_attempt,ordinal),
        FOREIGN KEY(run_id,phase_attempt)
            REFERENCES odds_weather_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_provider_events (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        provider_event_id TEXT NOT NULL CHECK (length(trim(provider_event_id))>0),
        retrieved_at TEXT NOT NULL CHECK ({_aware('retrieved_at')}),
        revision_ordinal INTEGER NOT NULL CHECK (revision_ordinal>=1),
        sport_key TEXT NOT NULL CHECK (sport_key='baseball_mlb'),
        commence_time TEXT NOT NULL CHECK ({_aware('commence_time')}),
        away_team_id TEXT NOT NULL CHECK (length(trim(away_team_id))>0),
        home_team_id TEXT NOT NULL CHECK (length(trim(home_team_id))>0),
        raw_capture_checksum TEXT NOT NULL CHECK ({_sha('raw_capture_checksum')}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_ODDS_PROVIDER_EVENT_V1'),
        event_checksum TEXT NOT NULL CHECK ({_sha('event_checksum')}),
        canonical_event_json TEXT NOT NULL CHECK (
            json_valid(canonical_event_json) AND json_type(canonical_event_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(run_id,phase_attempt,provider_event_id,retrieved_at),
        UNIQUE(run_id,phase_attempt,provider_event_id,revision_ordinal),
        FOREIGN KEY(run_id,phase_attempt,raw_capture_checksum)
            REFERENCES odds_weather_raw_captures(
                run_id,phase_attempt,raw_capture_checksum
            ) ON DELETE RESTRICT,
        CHECK (away_team_id<>home_team_id)
    )
    """,
    f"""
    CREATE TABLE odds_weather_bookmakers (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        provider_event_id TEXT NOT NULL,
        event_retrieved_at TEXT NOT NULL CHECK ({_aware('event_retrieved_at')}),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        bookmaker_key TEXT NOT NULL CHECK (length(trim(bookmaker_key))>0),
        title TEXT NOT NULL CHECK (length(trim(title))>0),
        bookmaker_last_update TEXT CHECK (
            bookmaker_last_update IS NULL OR ({_aware('bookmaker_last_update')})
        ),
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key
        ),
        UNIQUE(run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal),
        FOREIGN KEY(run_id,phase_attempt,provider_event_id,event_retrieved_at)
            REFERENCES odds_weather_provider_events(
                run_id,phase_attempt,provider_event_id,retrieved_at
            ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_markets (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        provider_event_id TEXT NOT NULL,
        event_retrieved_at TEXT NOT NULL CHECK ({_aware('event_retrieved_at')}),
        bookmaker_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        market_key TEXT NOT NULL CHECK (market_key IN ('h2h','spreads','totals')),
        market_last_update TEXT CHECK (
            market_last_update IS NULL OR ({_aware('market_last_update')})
        ),
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,market_key
        ),
        UNIQUE(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,ordinal
        ),
        FOREIGN KEY(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key
        ) REFERENCES odds_weather_bookmakers(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key
        ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_outcomes (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        provider_event_id TEXT NOT NULL,
        event_retrieved_at TEXT NOT NULL CHECK ({_aware('event_retrieved_at')}),
        bookmaker_key TEXT NOT NULL,
        market_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        outcome_name TEXT NOT NULL CHECK (length(trim(outcome_name))>0),
        price_american REAL NOT NULL,
        point REAL,
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,market_key,outcome_name
        ),
        UNIQUE(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,market_key,ordinal
        ),
        FOREIGN KEY(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,market_key
        ) REFERENCES odds_weather_markets(
            run_id,phase_attempt,provider_event_id,event_retrieved_at,
            bookmaker_key,market_key
        ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_odds_revisions (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        provider_event_id TEXT NOT NULL,
        event_retrieved_at TEXT NOT NULL CHECK ({_aware('event_retrieved_at')}),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        bookmaker_key TEXT NOT NULL CHECK (length(trim(bookmaker_key))>0),
        market_key TEXT NOT NULL CHECK (market_key IN ('h2h','spreads','totals')),
        outcome_name TEXT NOT NULL CHECK (length(trim(outcome_name))>0),
        price_american REAL NOT NULL,
        point REAL,
        provider_last_update TEXT CHECK (
            provider_last_update IS NULL OR ({_aware('provider_last_update')})
        ),
        bookmaker_last_update TEXT CHECK (
            bookmaker_last_update IS NULL OR ({_aware('bookmaker_last_update')})
        ),
        market_last_update TEXT CHECK (
            market_last_update IS NULL OR ({_aware('market_last_update')})
        ),
        retrieved_at TEXT NOT NULL CHECK ({_aware('retrieved_at')}),
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal),
        FOREIGN KEY(run_id,phase_attempt,provider_event_id,event_retrieved_at)
            REFERENCES odds_weather_provider_events(
                run_id,phase_attempt,provider_event_id,retrieved_at
            ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_weather_revisions (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        source_game_id TEXT NOT NULL CHECK (length(trim(source_game_id))>0),
        provider TEXT NOT NULL CHECK (provider IN ('nws','openweather')),
        retrieved_at TEXT NOT NULL CHECK ({_aware('retrieved_at')}),
        revision_ordinal INTEGER NOT NULL CHECK (revision_ordinal>=1),
        forecast_time TEXT NOT NULL CHECK ({_aware('forecast_time')}),
        forecast_offset_minutes REAL CHECK (
            forecast_offset_minutes IS NULL
            OR (forecast_offset_minutes>=0 AND forecast_offset_minutes<=60)
        ),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_WEATHER_FORECAST_V1'),
        forecast_checksum TEXT NOT NULL CHECK ({_sha('forecast_checksum')}),
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(run_id,phase_attempt,source_game_id,provider,retrieved_at),
        UNIQUE(run_id,phase_attempt,source_game_id,provider,revision_ordinal),
        FOREIGN KEY(run_id,phase_attempt)
            REFERENCES odds_weather_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_weather_raw_captures (
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        source_game_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        weather_retrieved_at TEXT NOT NULL CHECK ({_aware('weather_retrieved_at')}),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        raw_capture_checksum TEXT NOT NULL CHECK ({_sha('raw_capture_checksum')}),
        PRIMARY KEY(
            run_id,phase_attempt,source_game_id,provider,
            weather_retrieved_at,raw_capture_checksum
        ),
        UNIQUE(
            run_id,phase_attempt,source_game_id,provider,
            weather_retrieved_at,ordinal
        ),
        FOREIGN KEY(
            run_id,phase_attempt,source_game_id,provider,weather_retrieved_at
        ) REFERENCES odds_weather_weather_revisions(
            run_id,phase_attempt,source_game_id,provider,retrieved_at
        ) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_attempt,raw_capture_checksum)
            REFERENCES odds_weather_raw_captures(
                run_id,phase_attempt,raw_capture_checksum
            ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_snapshot_raw_captures (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        raw_capture_checksum TEXT NOT NULL CHECK ({_sha('raw_capture_checksum')}),
        PRIMARY KEY(snapshot_id,raw_capture_checksum),
        UNIQUE(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt)
            REFERENCES odds_weather_snapshots(
                snapshot_id,run_id,phase_attempt
            ) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_attempt,raw_capture_checksum)
            REFERENCES odds_weather_raw_captures(
                run_id,phase_attempt,raw_capture_checksum
            ) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        edge_event_id TEXT NOT NULL CHECK (length(trim(edge_event_id))>0),
        daily_mlb_game_id TEXT NOT NULL CHECK (length(trim(daily_mlb_game_id))>0),
        source_game_id TEXT NOT NULL CHECK (length(trim(source_game_id))>0),
        official_date TEXT NOT NULL CHECK (official_date {_DATE_CHECK}),
        away_team_id TEXT NOT NULL CHECK (length(trim(away_team_id))>0),
        home_team_id TEXT NOT NULL CHECK (length(trim(home_team_id))>0),
        source_away_team_id TEXT NOT NULL CHECK (length(trim(source_away_team_id))>0),
        source_home_team_id TEXT NOT NULL CHECK (length(trim(source_home_team_id))>0),
        venue_id TEXT,
        game_status TEXT NOT NULL CHECK (
            game_status IN (
                'scheduled','pregame','in_progress','delayed','postponed',
                'suspended','final','cancelled','unknown'
            )
        ),
        scheduled_start_time TEXT CHECK (
            scheduled_start_time IS NULL OR ({_aware('scheduled_start_time')})
        ),
        upstream_daily_slate_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_game_checksum')}),
        upstream_game_state_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_game_checksum')}),
        upstream_baseball_intelligence_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_game_checksum')}),
        odds_availability TEXT NOT NULL CHECK (odds_availability IN ('available','unavailable')),
        selected_provider_event_id TEXT,
        odds_retrieved_at TEXT CHECK (
            odds_retrieved_at IS NULL OR ({_aware('odds_retrieved_at')})
        ),
        odds_raw_capture_checksum TEXT CHECK (
            odds_raw_capture_checksum IS NULL OR ({_sha('odds_raw_capture_checksum')})
        ),
        event_match_offset_minutes REAL CHECK (
            event_match_offset_minutes IS NULL OR event_match_offset_minutes>=0
        ),
        odds_summary_json TEXT CHECK (odds_summary_json IS NULL OR json_valid(odds_summary_json)),
        odds_summary_checksum TEXT CHECK (
            odds_summary_checksum IS NULL OR ({_sha('odds_summary_checksum')})
        ),
        normalized_market_count INTEGER NOT NULL CHECK (normalized_market_count>=0),
        raw_snapshot_count INTEGER NOT NULL CHECK (raw_snapshot_count>=0),
        freshness_counts_json TEXT NOT NULL CHECK (
            json_valid(freshness_counts_json) AND json_type(freshness_counts_json)='object'
        ),
        odds_calculation_version TEXT NOT NULL CHECK (odds_calculation_version='odds-v3-provider-snapshots'),
        odds_consensus_contract_version TEXT NOT NULL CHECK (odds_consensus_contract_version='odds-consensus-v2'),
        weather_status TEXT NOT NULL CHECK (
            weather_status IN ('available','indoor_fixed_roof','unavailable')
        ),
        weather_relevance TEXT NOT NULL CHECK (
            weather_relevance IN (
                'direct','contextual_roof_status_unknown',
                'contextual_roof_type_unverified','indoor_suppressed','unavailable'
            )
        ),
        weather_primary_source TEXT CHECK (
            weather_primary_source IS NULL OR weather_primary_source IN ('nws','openweather')
        ),
        venue_context_json TEXT CHECK (venue_context_json IS NULL OR json_valid(venue_context_json)),
        venue_context_checksum TEXT CHECK (
            venue_context_checksum IS NULL OR ({_sha('venue_context_checksum')})
        ),
        weather_comparison_json TEXT NOT NULL CHECK (
            json_valid(weather_comparison_json) AND json_type(weather_comparison_json)='object'
        ),
        baseball_wind_impact_json TEXT NOT NULL CHECK (
            json_valid(baseball_wind_impact_json) AND json_type(baseball_wind_impact_json)='object'
        ),
        weather_revision_count INTEGER NOT NULL CHECK (weather_revision_count>=0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(snapshot_id,edge_event_id),
        UNIQUE(snapshot_id,ordinal),
        UNIQUE(snapshot_id,daily_mlb_game_id),
        UNIQUE(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,selected_provider_event_id),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt)
            REFERENCES odds_weather_snapshots(
                snapshot_id,run_id,phase_attempt
            ) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_attempt,selected_provider_event_id,odds_retrieved_at)
            REFERENCES odds_weather_provider_events(
                run_id,phase_attempt,provider_event_id,retrieved_at
            ) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_attempt,odds_raw_capture_checksum)
            REFERENCES odds_weather_raw_captures(
                run_id,phase_attempt,raw_capture_checksum
            ) ON DELETE RESTRICT,
        CHECK (away_team_id<>home_team_id),
        CHECK (
            (
                odds_availability='available'
                AND selected_provider_event_id IS NOT NULL
                AND odds_retrieved_at IS NOT NULL
                AND odds_raw_capture_checksum IS NOT NULL
                AND event_match_offset_minutes IS NOT NULL
                AND odds_summary_json IS NOT NULL
                AND odds_summary_checksum IS NOT NULL
            ) OR (
                odds_availability='unavailable'
                AND selected_provider_event_id IS NULL
                AND odds_retrieved_at IS NULL
                AND odds_raw_capture_checksum IS NULL
                AND event_match_offset_minutes IS NULL
                AND odds_summary_json IS NULL
                AND odds_summary_checksum IS NULL
                AND normalized_market_count=0
                AND raw_snapshot_count=0
            )
        ),
        CHECK (
            (weather_status='available' AND weather_primary_source IS NOT NULL AND weather_revision_count>=1)
            OR (weather_status<>'available' AND weather_primary_source IS NULL AND weather_revision_count=0)
        )
    )
    """,
    f"""
    CREATE TABLE odds_weather_warnings (
        snapshot_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        code TEXT NOT NULL CHECK (length(trim(code))>0),
        domain TEXT NOT NULL CHECK (domain IN ('odds_matching','odds','weather','stadium')),
        message TEXT NOT NULL CHECK (length(trim(message))>0),
        source_game_id TEXT,
        provider TEXT,
        provider_event_id TEXT,
        canonical_json TEXT NOT NULL CHECK (
            json_valid(canonical_json) AND json_type(canonical_json)='object'
        ),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id)
            REFERENCES odds_weather_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE odds_weather_game_weather_selections (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        source_game_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider IN ('nws','openweather')),
        weather_retrieved_at TEXT NOT NULL CHECK ({_aware('weather_retrieved_at')}),
        forecast_checksum TEXT NOT NULL CHECK ({_sha('forecast_checksum')}),
        is_primary INTEGER NOT NULL CHECK (is_primary IN (0,1)),
        PRIMARY KEY(snapshot_id,source_game_id,provider),
        FOREIGN KEY(snapshot_id,source_game_id)
            REFERENCES odds_weather_games(snapshot_id,source_game_id) ON DELETE RESTRICT,
        FOREIGN KEY(
            run_id,phase_attempt,source_game_id,provider,weather_retrieved_at
        ) REFERENCES odds_weather_weather_revisions(
            run_id,phase_attempt,source_game_id,provider,retrieved_at
        ) ON DELETE RESTRICT
    )
    """,
)


ODDS_WEATHER_SCHEMA_V11_INDEX_STATEMENTS = (
    "CREATE INDEX idx_ow_attempt_lookup ON odds_weather_attempt_evidence(run_id,requested_date,outcome,phase_attempt)",
    "CREATE INDEX idx_ow_attempt_cutoff ON odds_weather_attempt_evidence(requested_date,observed_at,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_snapshots_run ON odds_weather_snapshots(run_id,phase_attempt,requested_date,snapshot_id)",
    "CREATE INDEX idx_ow_snapshots_upstream_bia ON odds_weather_snapshots(upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,snapshot_id)",
    "CREATE INDEX idx_ow_snapshots_cutoff ON odds_weather_snapshots(requested_date,observed_at,snapshot_id)",
    "CREATE INDEX idx_ow_snapshots_artifact ON odds_weather_snapshots(artifact_checksum,artifact_relpath,snapshot_id)",
    "CREATE INDEX idx_ow_games_source ON odds_weather_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_ow_games_provider_event ON odds_weather_games(selected_provider_event_id,odds_retrieved_at,snapshot_id) WHERE selected_provider_event_id IS NOT NULL",
    "CREATE INDEX idx_ow_warnings_lookup ON odds_weather_warnings(snapshot_id,source_game_id,domain,ordinal)",
    "CREATE INDEX idx_ow_raw_captures_provider ON odds_weather_raw_captures(provider,endpoint_category,retrieved_at,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_provider_events_cutoff ON odds_weather_provider_events(provider_event_id,retrieved_at,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_provider_events_match ON odds_weather_provider_events(away_team_id,home_team_id,commence_time,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_bookmakers_key ON odds_weather_bookmakers(bookmaker_key,event_retrieved_at,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_markets_key_update ON odds_weather_markets(market_key,market_last_update,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_outcomes_identity ON odds_weather_outcomes(outcome_name,market_key,bookmaker_key,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_odds_revisions_cutoff ON odds_weather_odds_revisions(provider_event_id,bookmaker_key,market_key,outcome_name,retrieved_at,run_id,phase_attempt)",
    "CREATE UNIQUE INDEX uq_ow_odds_revisions_unpointed_identity ON odds_weather_odds_revisions(run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key,market_key,outcome_name,retrieved_at) WHERE point IS NULL",
    "CREATE UNIQUE INDEX uq_ow_odds_revisions_pointed_identity ON odds_weather_odds_revisions(run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key,market_key,outcome_name,retrieved_at,point) WHERE point IS NOT NULL",
    "CREATE INDEX idx_ow_weather_revisions_cutoff ON odds_weather_weather_revisions(source_game_id,provider,forecast_time,retrieved_at,run_id,phase_attempt)",
    "CREATE INDEX idx_ow_weather_selections_provider ON odds_weather_game_weather_selections(provider,weather_retrieved_at,snapshot_id,source_game_id)",
)


ODDS_WEATHER_SCHEMA_V11_VALIDATION_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER odds_weather_attempt_evidence_validate_phase
    BEFORE INSERT ON odds_weather_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM pipeline_runs run
        JOIN pipeline_run_phases phase
          ON phase.run_id=run.run_id AND phase.phase_key='odds_weather'
        JOIN daily_slate_snapshots slate
          ON slate.snapshot_id=NEW.upstream_daily_slate_snapshot_id
        JOIN game_state_snapshots state
          ON state.snapshot_id=NEW.upstream_game_state_snapshot_id
        JOIN baseball_intelligence_snapshots bia
          ON bia.snapshot_id=NEW.upstream_baseball_intelligence_snapshot_id
        WHERE run.run_id=NEW.run_id
          AND run.requested_date=NEW.requested_date
          AND run.as_of_time=NEW.as_of_time
          AND phase.status='running'
          AND phase.attempt_count=NEW.phase_attempt
          AND slate.run_id=NEW.run_id
          AND slate.requested_date=NEW.requested_date
          AND slate.as_of_time=NEW.as_of_time
          AND slate.sealed_at IS NOT NULL
          AND slate.snapshot_checksum=NEW.upstream_daily_slate_checksum
          AND state.run_id=NEW.run_id
          AND state.requested_date=NEW.requested_date
          AND state.as_of_time=NEW.as_of_time
          AND state.sealed_at IS NOT NULL
          AND state.snapshot_checksum=NEW.upstream_game_state_checksum
          AND state.upstream_daily_slate_snapshot_id=slate.snapshot_id
          AND state.upstream_daily_slate_checksum=slate.snapshot_checksum
          AND bia.run_id=NEW.run_id
          AND bia.requested_date=NEW.requested_date
          AND bia.as_of_time=NEW.as_of_time
          AND bia.sealed_at IS NOT NULL
          AND bia.assembly_checksum=NEW.upstream_baseball_intelligence_checksum
          AND bia.upstream_daily_slate_snapshot_id=slate.snapshot_id
          AND bia.upstream_daily_slate_checksum=slate.snapshot_checksum
          AND bia.upstream_game_state_snapshot_id=state.snapshot_id
          AND bia.upstream_game_state_checksum=state.snapshot_checksum
      ) THEN RAISE(ABORT,'Odds Weather attempt requires active phase and exact sealed upstream chain') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_snapshots_validate_phase
    BEFORE INSERT ON odds_weather_snapshots BEGIN
      SELECT CASE WHEN NEW.sealed_at IS NOT NULL
        THEN RAISE(ABORT,'Odds Weather snapshot must begin unsealed') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM odds_weather_attempt_evidence attempt
        JOIN pipeline_run_phases phase
          ON phase.run_id=attempt.run_id AND phase.phase_key='odds_weather'
        JOIN daily_slate_snapshots slate
          ON slate.snapshot_id=attempt.upstream_daily_slate_snapshot_id
        JOIN game_state_snapshots state
          ON state.snapshot_id=attempt.upstream_game_state_snapshot_id
        JOIN baseball_intelligence_snapshots bia
          ON bia.snapshot_id=attempt.upstream_baseball_intelligence_snapshot_id
        WHERE attempt.run_id=NEW.run_id
          AND attempt.phase_attempt=NEW.phase_attempt
          AND attempt.requested_date=NEW.requested_date
          AND attempt.as_of_time=NEW.as_of_time
          AND attempt.observed_at=NEW.observed_at
          AND attempt.phase_input_checksum=NEW.phase_input_checksum
          AND attempt.outcome='assembled'
          AND attempt.snapshot_checksum=NEW.snapshot_checksum
          AND attempt.upstream_daily_slate_snapshot_id=NEW.upstream_daily_slate_snapshot_id
          AND attempt.upstream_daily_slate_checksum=NEW.upstream_daily_slate_checksum
          AND attempt.upstream_game_state_snapshot_id=NEW.upstream_game_state_snapshot_id
          AND attempt.upstream_game_state_checksum=NEW.upstream_game_state_checksum
          AND attempt.upstream_baseball_intelligence_snapshot_id=NEW.upstream_baseball_intelligence_snapshot_id
          AND attempt.upstream_baseball_intelligence_checksum=NEW.upstream_baseball_intelligence_checksum
          AND phase.status='running'
          AND phase.attempt_count=NEW.phase_attempt
          AND slate.sealed_at IS NOT NULL
          AND state.sealed_at IS NOT NULL
          AND bia.sealed_at IS NOT NULL
      ) THEN RAISE(ABORT,'Odds Weather snapshot requires matching assembled attempt and sealed lineage') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_raw_captures_validate_active_attempt
    BEFORE INSERT ON odds_weather_raw_captures BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_attempt_evidence attempt
        JOIN pipeline_run_phases phase
          ON phase.run_id=attempt.run_id AND phase.phase_key='odds_weather'
        WHERE attempt.run_id=NEW.run_id
          AND attempt.phase_attempt=NEW.phase_attempt
          AND phase.status='running'
          AND phase.attempt_count=NEW.phase_attempt
      ) THEN RAISE(ABORT,'Odds Weather raw capture requires active attempt') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_provider_events_validate_active_attempt
    BEFORE INSERT ON odds_weather_provider_events BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_attempt_evidence attempt
        JOIN pipeline_run_phases phase
          ON phase.run_id=attempt.run_id AND phase.phase_key='odds_weather'
        WHERE attempt.run_id=NEW.run_id
          AND attempt.phase_attempt=NEW.phase_attempt
          AND phase.status='running'
          AND phase.attempt_count=NEW.phase_attempt
      ) THEN RAISE(ABORT,'Odds Weather provider event requires active attempt') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_weather_revisions_validate_upstream
    BEFORE INSERT ON odds_weather_weather_revisions BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_attempt_evidence attempt
        JOIN pipeline_run_phases phase
          ON phase.run_id=attempt.run_id AND phase.phase_key='odds_weather'
        JOIN baseball_intelligence_games game
          ON game.snapshot_id=attempt.upstream_baseball_intelligence_snapshot_id
         AND game.source_game_id=NEW.source_game_id
        WHERE attempt.run_id=NEW.run_id
          AND attempt.phase_attempt=NEW.phase_attempt
          AND phase.status='running'
          AND phase.attempt_count=NEW.phase_attempt
      ) THEN RAISE(ABORT,'Odds Weather weather revision requires active attempt and upstream game') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_games_validate_upstream
    BEFORE INSERT ON odds_weather_games BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM odds_weather_snapshots snapshot
        JOIN daily_slate_games slate
          ON slate.snapshot_id=snapshot.upstream_daily_slate_snapshot_id
         AND slate.source_game_id=NEW.source_game_id
        JOIN game_state_games state
          ON state.snapshot_id=snapshot.upstream_game_state_snapshot_id
         AND state.source_game_id=NEW.source_game_id
        JOIN baseball_intelligence_games bia
          ON bia.snapshot_id=snapshot.upstream_baseball_intelligence_snapshot_id
         AND bia.source_game_id=NEW.source_game_id
        WHERE snapshot.snapshot_id=NEW.snapshot_id
          AND snapshot.run_id=NEW.run_id
          AND snapshot.phase_attempt=NEW.phase_attempt
          AND snapshot.sealed_at IS NULL
          AND slate.ordinal=NEW.ordinal
          AND state.ordinal=NEW.ordinal
          AND bia.ordinal=NEW.ordinal
          AND slate.edge_event_id=NEW.edge_event_id
          AND state.edge_event_id=NEW.edge_event_id
          AND bia.edge_event_id=NEW.edge_event_id
          AND slate.daily_mlb_game_id=NEW.daily_mlb_game_id
          AND state.daily_mlb_game_id=NEW.daily_mlb_game_id
          AND bia.daily_mlb_game_id=NEW.daily_mlb_game_id
          AND slate.away_team_id=NEW.away_team_id
          AND state.away_team_id=NEW.away_team_id
          AND bia.away_team_id=NEW.away_team_id
          AND slate.home_team_id=NEW.home_team_id
          AND state.home_team_id=NEW.home_team_id
          AND bia.home_team_id=NEW.home_team_id
          AND slate.official_date=NEW.official_date
          AND json_extract(bia.canonical_json,'$.away.source_team_id')=NEW.source_away_team_id
          AND json_extract(bia.canonical_json,'$.home.source_team_id')=NEW.source_home_team_id
          AND bia.venue_id IS NEW.venue_id
          AND bia.game_status=NEW.game_status
          AND json_extract(slate.canonical_json,'$.scheduled_start_time') IS NEW.scheduled_start_time
          AND slate.row_checksum=NEW.upstream_daily_slate_game_checksum
          AND state.row_checksum=NEW.upstream_game_state_game_checksum
          AND bia.row_checksum=NEW.upstream_baseball_intelligence_game_checksum
      ) THEN RAISE(ABORT,'Odds Weather game requires exact ordered upstream game lineage') END;
      SELECT CASE WHEN NEW.odds_availability='available' AND NOT EXISTS (
        SELECT 1 FROM odds_weather_provider_events event
        WHERE event.run_id=NEW.run_id
          AND event.phase_attempt=NEW.phase_attempt
          AND event.provider_event_id=NEW.selected_provider_event_id
          AND event.retrieved_at=NEW.odds_retrieved_at
          AND event.raw_capture_checksum=NEW.odds_raw_capture_checksum
          AND event.away_team_id=NEW.away_team_id
          AND event.home_team_id=NEW.home_team_id
      ) THEN RAISE(ABORT,'Odds Weather selected event requires exact same-attempt provider evidence') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_snapshot_children_validate_unsealed
    BEFORE INSERT ON odds_weather_snapshot_raw_captures BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_snapshots snapshot
        WHERE snapshot.snapshot_id=NEW.snapshot_id
          AND snapshot.run_id=NEW.run_id
          AND snapshot.phase_attempt=NEW.phase_attempt
          AND snapshot.sealed_at IS NULL
      ) THEN RAISE(ABORT,'Odds Weather selected raw capture requires unsealed snapshot') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_warnings_validate_unsealed
    BEFORE INSERT ON odds_weather_warnings BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_snapshots snapshot
        WHERE snapshot.snapshot_id=NEW.snapshot_id AND snapshot.sealed_at IS NULL
      ) THEN RAISE(ABORT,'Odds Weather warning requires unsealed snapshot') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_weather_selections_validate_unsealed
    BEFORE INSERT ON odds_weather_game_weather_selections BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM odds_weather_games game
        JOIN odds_weather_snapshots snapshot ON snapshot.snapshot_id=game.snapshot_id
        JOIN odds_weather_weather_revisions revision
          ON revision.run_id=NEW.run_id
         AND revision.phase_attempt=NEW.phase_attempt
         AND revision.source_game_id=NEW.source_game_id
         AND revision.provider=NEW.provider
         AND revision.retrieved_at=NEW.weather_retrieved_at
        WHERE game.snapshot_id=NEW.snapshot_id
          AND game.source_game_id=NEW.source_game_id
          AND game.run_id=NEW.run_id
          AND game.phase_attempt=NEW.phase_attempt
          AND snapshot.sealed_at IS NULL
          AND revision.forecast_checksum=NEW.forecast_checksum
      ) THEN RAISE(ABORT,'Odds Weather weather selection requires exact unsealed same-attempt evidence') END;
    END
    """,
    """
    CREATE TRIGGER odds_weather_snapshots_validate_seal
    BEFORE UPDATE OF sealed_at ON odds_weather_snapshots BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL
        OR length(trim(NEW.sealed_at))=0
        THEN RAISE(ABORT,'Odds Weather snapshot sealing is one-time and nonblank') END;
      SELECT CASE WHEN
        NEW.snapshot_id<>OLD.snapshot_id
        OR NEW.run_id<>OLD.run_id
        OR NEW.phase_attempt<>OLD.phase_attempt
        OR NEW.snapshot_checksum<>OLD.snapshot_checksum
        OR NEW.canonical_json<>OLD.canonical_json
        THEN RAISE(ABORT,'Odds Weather snapshot seal cannot mutate evidence') END;
      SELECT CASE WHEN
        (SELECT count(*) FROM odds_weather_games game WHERE game.snapshot_id=OLD.snapshot_id)<>OLD.game_count
        OR (SELECT count(*) FROM odds_weather_warnings warning WHERE warning.snapshot_id=OLD.snapshot_id)<>OLD.warning_count
        OR (SELECT count(*) FROM odds_weather_snapshot_raw_captures raw WHERE raw.snapshot_id=OLD.snapshot_id)<>OLD.selected_raw_capture_count
        OR (SELECT count(*) FROM odds_weather_game_weather_selections selected WHERE selected.snapshot_id=OLD.snapshot_id)<>OLD.weather_selection_count
        OR json_array_length(json_extract(OLD.canonical_json,'$.games'))<>OLD.game_count
        OR json_array_length(json_extract(OLD.canonical_json,'$.warnings'))<>OLD.warning_count
        OR json_array_length(OLD.source_raw_capture_checksums_json)<>OLD.selected_raw_capture_count
        OR (SELECT count(*) FROM daily_slate_games game WHERE game.snapshot_id=OLD.upstream_daily_slate_snapshot_id)<>OLD.game_count
        OR (SELECT count(*) FROM game_state_games game WHERE game.snapshot_id=OLD.upstream_game_state_snapshot_id)<>OLD.game_count
        OR (SELECT count(*) FROM baseball_intelligence_games game WHERE game.snapshot_id=OLD.upstream_baseball_intelligence_snapshot_id)<>OLD.game_count
        OR (OLD.game_count>0 AND (
            (SELECT min(ordinal) FROM odds_weather_games WHERE snapshot_id=OLD.snapshot_id)<>1
            OR (SELECT max(ordinal) FROM odds_weather_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
            OR (SELECT count(DISTINCT ordinal) FROM odds_weather_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
        ))
        OR (OLD.warning_count>0 AND (
            (SELECT min(ordinal) FROM odds_weather_warnings WHERE snapshot_id=OLD.snapshot_id)<>1
            OR (SELECT max(ordinal) FROM odds_weather_warnings WHERE snapshot_id=OLD.snapshot_id)<>OLD.warning_count
        ))
        OR (OLD.selected_raw_capture_count>0 AND (
            (SELECT min(ordinal) FROM odds_weather_snapshot_raw_captures WHERE snapshot_id=OLD.snapshot_id)<>1
            OR (SELECT max(ordinal) FROM odds_weather_snapshot_raw_captures WHERE snapshot_id=OLD.snapshot_id)<>OLD.selected_raw_capture_count
        ))
        OR EXISTS (
            SELECT 1 FROM odds_weather_games game
            WHERE game.snapshot_id=OLD.snapshot_id
              AND (
                (game.weather_status='available' AND (
                    (SELECT count(*) FROM odds_weather_game_weather_selections selected
                     WHERE selected.snapshot_id=game.snapshot_id
                       AND selected.source_game_id=game.source_game_id)<>game.weather_revision_count
                    OR (SELECT count(*) FROM odds_weather_game_weather_selections selected
                        WHERE selected.snapshot_id=game.snapshot_id
                          AND selected.source_game_id=game.source_game_id
                          AND selected.is_primary=1)<>1
                    OR NOT EXISTS (
                        SELECT 1 FROM odds_weather_game_weather_selections selected
                        WHERE selected.snapshot_id=game.snapshot_id
                          AND selected.source_game_id=game.source_game_id
                          AND selected.provider=game.weather_primary_source
                          AND selected.is_primary=1
                    )
                ))
                OR (game.weather_status<>'available' AND EXISTS (
                    SELECT 1 FROM odds_weather_game_weather_selections selected
                    WHERE selected.snapshot_id=game.snapshot_id
                      AND selected.source_game_id=game.source_game_id
                ))
              )
        )
        THEN RAISE(ABORT,'Odds Weather snapshot children or upstream coverage are incomplete') END;
    END
    """,
)


_IMMUTABLE_TABLES = (
    "odds_weather_attempt_evidence",
    "odds_weather_games",
    "odds_weather_warnings",
    "odds_weather_raw_captures",
    "odds_weather_snapshot_raw_captures",
    "odds_weather_provider_events",
    "odds_weather_bookmakers",
    "odds_weather_markets",
    "odds_weather_outcomes",
    "odds_weather_odds_revisions",
    "odds_weather_weather_revisions",
    "odds_weather_weather_raw_captures",
    "odds_weather_game_weather_selections",
)


ODDS_WEATHER_SCHEMA_V11_IMMUTABILITY_TRIGGER_STATEMENTS = tuple(
    statement
    for table in _IMMUTABLE_TABLES
    for statement in (
        f"CREATE TRIGGER {table}_reject_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Odds Weather retained evidence is immutable'); END",
        f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Odds Weather retained evidence cannot be deleted'); END",
    )
) + (
    "CREATE TRIGGER odds_weather_snapshots_reject_update BEFORE UPDATE OF snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,sport,league,contract_version,phase_input_checksum,upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,upstream_game_state_snapshot_id,upstream_game_state_checksum,upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,snapshot_checksum,source_raw_capture_checksums_json,warnings_json,warning_count,canonical_json,game_count,selected_raw_capture_count,weather_selection_count,artifact_relpath,artifact_checksum,artifact_byte_count,created_at ON odds_weather_snapshots BEGIN SELECT RAISE(ABORT,'Odds Weather snapshots are immutable'); END",
    "CREATE TRIGGER odds_weather_snapshots_reject_delete BEFORE DELETE ON odds_weather_snapshots BEGIN SELECT RAISE(ABORT,'Odds Weather snapshots cannot be deleted'); END",
)


ODDS_WEATHER_SCHEMA_V11_STATEMENTS = (
    *ODDS_WEATHER_SCHEMA_V11_TABLE_STATEMENTS,
    *ODDS_WEATHER_SCHEMA_V11_INDEX_STATEMENTS,
    *ODDS_WEATHER_SCHEMA_V11_VALIDATION_TRIGGER_STATEMENTS,
    *ODDS_WEATHER_SCHEMA_V11_IMMUTABILITY_TRIGGER_STATEMENTS,
)
