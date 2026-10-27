# Development Plan

Current status: **0.4C.6**. Every step is archived in a flat `backup_before_<version>[_timestamp]/` folder containing only the files changed at that step.

## Completed

- [x] 0.3A — Core Console background pilot (A/00 DWGs, QSAVE-only engine)
- [x] 0.3B–0.3C — Destination tree, supporting-file clone, atomic copies
- [x] 0.4A.2 — Title-block discovery (`title_block_discovery_fields.csv`)
- [x] 0.4B.1a — Spatial revision plan from discovered fields
- [x] 0.4B.2.5.2 — Complete row-band cleanup of non-output revision rows
- [x] 0.4B.2.7 — Title-block completion actions (dates, contractor doc-id, current revision)
- [x] 0.4C.0.1 — Stable external writer (the live writer)
- [x] 0.4C.1 / 0.4C.3.2 — Reopen validators
- [x] 0.4C.2A/2B — Atomic publish dry-run and publisher
- [x] 0.4C.2D–2F.1 — Production-scale workers, adaptive detection, row-00 recovery
- [x] 0.4C.3–0.4C.4.1 — PDF publisher (ActiveX implementation; did not work in Core Console)
- [x] 0.4C.5 — Command-line `-PLOT` PDF publisher, settings persistence fix, Reports tab, first tests
- [x] 0.4C.6 — Pipeline-first UI (one primary action up to the publish gate), parallel writer/validator/PDF stages, PDF skip-on-rerun and workers setting

## Next

### 0.4D — Revision-cloud / obsolete-entity removal

Last structural item from the original scope. Today the writer can only write values/layers or verify; `cleanup_plan.py` clears revision-table cells but never deletes drawing entities.

- Add a `DELETE_ENTITY` action type to `stable_external_writer.lsp` and the Python writer.
- Accept only explicit, plan-generated handles (never layer-wide erase) and record counts in the plan/report.
- Extend the reopen validator to expect the entity to be gone and to detect accidental extra deletions.
- Require a dry-run summary of deletion counts before atomic publishing.

### 0.4E — DXF content migration

DXF is currently only copied to `00/5. NC1 & DXF`.

- Reuse the discovery → plan → write → validate pattern for DXF text entities.
- Keep NC1 and Part List behavior copy-only.
- Produce a separate DXF report and gate it behind its own explicit user action.

### 0.5 — Hardening and packaging

- Expand the `unittest` suite around the planner/action generation and checkpoint reuse.
- Add end-to-end fixture runs that do not require AutoCAD.
- Decide packaging (PyInstaller or portable venv) and document deployment.
- Tune the worker default on larger bundles (measure 100+ pairs) and consider plotting A/00 of one pair in parallel.

## Known issues and decisions

- **ActiveX is unavailable in Core Console.** `(vlax-get-acad-object)` returns `nil` (verified on AutoCAD 2027; the 2018 report shows the same `ACAD_OBJECT_NOT_AVAILABLE`). Do not reintroduce `vla-*` plotting; `lisp/pdf_modelspace_plotter.lsp` now uses `-PLOT`.
- **Paper-size names are exact.** `ISO_A1` is invalid; `ISO A1 (841.00 x 594.00 MM)` is the real name. Keep `preferred_media_names` in sync with the installed plotter.
- **The dead 0.3A pilot path was removed** in 0.4C.5: the permanently disabled pilot button, its worker and the Failure Queue tab. `autocad/background.py` remains as a legacy reference only.
- **Legacy revision modules removed in 0.4C.11** (`controlled_writer.py`, `cleanup_writer.py`, `responsive_validator.py`, `autocad/background.py`, plus their two LSPs). They were dead imports in `app.py`. The live writer is `stable_external_writer.py`.
- **`config/settings.json` is machine-local user state** and is rewritten by every button press. It is not a fixture.
- **Concurrency is the throughput lever.** Core Console startup is ~5.5 s of a ~6 s plot, so 0.4C.6 runs independent pairs in a bounded `ThreadPoolExecutor` (`run_many` / `validate_many` / `publish_many`). Result lists are re-sorted into plan order; `on_result` fires in completion order. `workers` comes from settings (`max_workers` in `revision/writer_settings.json` and `revision/pdf_settings.json` are only fallbacks).
- **PDF reruns are incremental.** `pdf_publish_report.json` stores `config_signature` (plotter/media/style/rotation/minimum size) plus each output's `source_sha256` and `pdf_sha256`. A pair is re-plotted only when one of those changes or the destination PDF is missing/hash-mismatched. The first pair is used as a plotter preflight only when no previous report matches the current signature.
- **One primary button is deliberate.** Don't re-add per-stage buttons to the main area; they live in the collapsed `Advanced` group, and publishing stays an explicit, confirmed action.
