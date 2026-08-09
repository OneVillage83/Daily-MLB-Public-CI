# Final QC V1 design

Phase 14 is deterministic evidence reconciliation, not analytics and not repair. Contract `DSE_FINAL_QC_V1` and frozen policy `DSE_FINAL_QC_POLICY_V1` require every check below to pass before a sealed QC snapshot exists:

1. `pdf_snapshot_exists`
2. `pdf_artifact_checksum`
3. `pdf_byte_count`
4. `pdf_preflight`
5. `infographic_snapshot_exists`
6. `infographic_artifact_checksums`
7. `infographic_dimensions`
8. `run_identity`
9. `game_inventory`
10. `recommendation_identity`
11. `recommendation_ranks`
12. `selected_identity`
13. `displayed_value_metrics`
14. `displayed_price_bookmakers`
15. `infographic_recommend_subset`
16. `infographic_rank_order`
17. `pick_of_day_rank_one`
18. `pass_not_promoted`
19. `avoid_not_promoted`
20. `no_unknown_recommendation`
21. `lineage_reconciled`
22. `timestamps_valid`
23. `paths_contained`
24. `artifact_sizes`
25. `secret_free`
26. `no_generation_exception`

A failed required check creates `validation_failed` attempt evidence with ordered observations and no QC snapshot. QC never edits upstream evidence. Valid zero-game and zero-recommendation output passes when every identity and artifact check reconciles.
