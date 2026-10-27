# AGENTS.md

Windows-only PySide6 desktop app that migrates Gamma AutoCAD drawings into the Nederwiek bundle structure by driving `accoreconsole.exe` (AutoCAD Core Console) with the AutoLISP files in `lisp/`. There is no CLI; the only tests are a stdlib `unittest` suite in `tests/`.

## Run & verify

- `run_app.bat` is the supported entrypoint: finds/installs `uv`, creates `.venv`, runs `uv pip install -r requirements.txt`, then `python app_modern.py` (falling back to `app.py` when that file is absent).
- Direct run once the venv exists: `.\.venv\Scripts\python.exe app.py` (venv is Python 3.12; code needs 3.10+ for `X | None` annotations). `app.py` takes no arguments — it only opens the GUI.
- Only dependency is `PySide6`. No pyproject, lockfile, linter, or formatter exists — do not invent one. Tests use the standard-library `unittest` runner (no pytest config).
- Fast syntax check that skips the 70+ backup trees:
  `.\.venv\Scripts\python.exe -m compileall -q app.py app_modern.py models.py scanner config autocad destination supporting titleblock revision runtime tests`
- Tests (no AutoCAD required):
  `.\.venv\Scripts\python.exe -m unittest discover -t . -s tests -v`
- Real verification is a manual GUI run. It requires a valid `accoreconsole.exe` path in `config/settings.json` and both PNGs in `resources/reference_images/`, otherwise pipeline stages abort with `*_NOT_FOUND` / missing-raster errors.

## Wiring

- `app.py` is the entire GUI plus every button handler, written as deliberately compressed one-liners (`;`-chained, minimal whitespace). Match that style when editing it; `runtime/` and `revision/` are normal PEP8 — don't reformat either.
- `app_modern.py` (`ModernMain(app.Main)`) is the presentation shell that `run_app.bat` launches: sidebar navigation and QSS over widgets the legacy class already builds. It must **not** re-define pipeline logic (`next_stage`, `_stage_complete`, `refresh_pipeline`, `_advance_pipeline`, `closeEvent`) — an earlier copy drifted from `app.py` and silently lost skipped-stage and `refresh_primary()` handling. `tests/test_app_shell_parity.py` enforces the rule. QSS selectors that target an object name (`QSpinBox#workerInput`, `QLabel#workerLabel`) require `setObjectName` in `_apply_widget_roles()`.
- **UI model (0.4C.6):** the Setup tab is one pipeline table (`PIPELINE`, `STAGE_REPORTS`, `AUTO_STAGES`) plus a single context-aware `primary` button. `refresh_pipeline()` derives each row's state from report existence and mtimes, and `next_stage()` returns the first missing or stale stage. `run_stage(key)` calls the matching handler and expects `True` when a background operation actually started, `False` otherwise; handlers that return a start flag must not return `None`. `_advance_pipeline()` chains `AUTO_STAGES` only — publishing and PDF stay explicit. Per-stage re-runs live in the collapsed `Advanced` group (`self.stage_buttons`); do not move them back into the main button row.
- Concurrency is per drawing pair, bounded by the Setup `Parallel workers` spinbox (`Settings.workers`, 1–16). The engines expose `run_many`/`validate_many`/`inspect_many`/`publish_many(..., workers, on_result, should_cancel)` and re-sort results into plan order; `on_result` fires in completion order, so never assume it is ordered. Keep `workers=1` paths working (they are the deterministic fallback used by tests). Dry-run and publish share `runtime/parallel.py::run_pool`.
- Long operations go through `runtime/background_controller.py`: `BackgroundOperationController.start(operation, payload)` dispatches to `PipelineWorker._run_<operation>()`, which imports its engine lazily. New backend work = add `_run_x` there plus the payload built in `app.py`.
- Anything that opens drawings runs `accoreconsole /i <dwg> /s <script> /l en-US` against copies staged under `%TEMP%\GNI\...`. Original source DWGs are read-only and must never be modified.
- Destination tree is fixed by `DestinationBuilder.FOLDERS` (16 folders). Both reference PNGs must be copied into every `*/CAD` folder and are re-copied by most stages.
- Shared primitives — never re-copy these into an engine: `runtime/console_text.py::decode_console` (Core Console output is UTF-16LE **without** a BOM; `utf-16` needs one), `runtime/hashing.py::sha256_file` (all checkpoint/publish guards must hash identically), `runtime/process_tracking.py::run_console` (registers the child so `closeEvent` can kill it). Each engine keeps a thin alias (`_decode`, `_sha256`, `_sha`) as `staticmethod(shared_fn)` — add an alias, not a new implementation; `tests/test_console_text.py` and `tests/test_hashing.py` assert identity.
- The stage gates (`load_ready_plans`, `load_writer_results`, `load_validated_pairs`, `load_ready_transactions`) all take `return_rejected=False`. With `True` they return `(accepted, rejected)` where `rejected` is a list of `{"identity", "errors"}`; they only raise when **nothing** is accepted. `app.py` records the rejects in `_GNI_REPORTS/gate_rejected.json`. A rejected item must never be silently promoted to accepted — publishing is destructive.

## Pipeline handoff (order matters)

Each stage consumes the previous stage's report from `<destination_root>/<bundle>/_GNI_REPORTS/`:

`scan` → destination build (`destination_manifest.json`) → supporting clone (`supporting_file_clone_report.json`, optional when nothing is selected) → `title_block_discovery_fields.csv` → `revision_pilot_write_plan.json` → `stable_external_writer_report.json` → `reopen_validation_report.json` → `atomic_publish_dry_run_report.json` → `atomic_publish_report.json` → `pdf_publish_report.json`.

- Buttons refuse to run when an upstream report is missing, so treat these filenames and schemas as a stable contract.
- `*_checkpoint.json` lets writer/validator resume; skipping is gated on SHA-256 of the source and working DWGs. Don't weaken that hash check. The writer hashes each plan source once per run (not per checkpoint write) — keep it that way.
- `pdf_publish_report.json` is also a resume record: `config_signature` plus per-output `source_sha256`/`pdf_sha256` let `_publish_pair` mark unchanged PDFs `PDF_UNCHANGED`. Only a signature change (plotter/media/style/rotation/minimum size) forces the first-pair preflight.
- Live planner is `revision/completion_plan.py` (`TitleBlockCompletionPlanBuilder` → `cleanup_plan` → `write_plan`). `revision/controlled_writer.py`, `cleanup_writer.py` and `responsive_validator.py` are legacy: only imported, not used, by current `app.py`. The live writer is `revision/stable_external_writer.py`.
- `revision/pdf_publisher.py` is Qt-free so it is testable; the `QThread` adapter lives in `revision/pdf_worker.py`. Keep that split when touching the PDF stage.

## Core Console / LISP rules

- Each generated `.scr` disables `FILEDIA`/`CMDDIA`, sets `TRUSTEDPATHS` to `%TEMP%\GNI\trusted_runtime`, and loads the LSP from there. LSPs are copied out of `lisp/` at runtime — edit `lisp/`, never the temp copy.
- Every Python driver asserts marker strings printed by its LSP (`GNI_BACKGROUND_ENGINE_0_3A_LOADED`, `GNI_STABLE_EXTERNAL_WRITER_0_4C_0_LOADED`, …). Changing a `(princ ...)` banner in a `.lsp` without updating the matching `LOAD_MARKER`/`VERSION_MARKER` constant in Python fails the run with `WRONG_LISP_VERSION` or `*_NOT_FOUND`.
- Console stdout/stderr is often UTF-16LE without BOM; use the existing `_decode`/`decode_console` helpers instead of `text=True`.
- Paths embedded in LISP use forward slashes. DWG writes are QSAVE-only (SAVEAS prompts/hangs), then publish atomically: temp file → size/hash verify → `os.replace`.
- **No ActiveX in Core Console.** `(vlax-get-acad-object)` returns `nil`, so `vla-*` calls (plotting, layout media queries) cannot work there. PDF plotting in `lisp/pdf_modelspace_plotter.lsp` uses the command-line `-PLOT` sequence; don't reintroduce `vla-*`.
- `-PLOT` prompts are positional and paper names must match the plotter exactly. `ISO_A1` is invalid; the real name is `ISO A1 (841.00 x 594.00 MM)`. Keep `revision/pdf_settings.json` (`preferred_media_names`, `plotter_candidates`, `plot_style`, `plot_rotation`) in sync with the installed plotter, and keep `PDF_PLOTTER_LOAD_MARKER` equal to the LISP banner.

## Versioning & backups

- Each change step is archived as a flat `backup_before_<version>[_timestamp]/` folder at repo root containing only the files changed at that moment. `.gitignore` lists them; keep them out of commits and searches.
- Before editing an existing file, copy it into a new `backup_before_<version>/` folder following the existing naming. Never delete or "clean up" existing backup folders.
- Search tools won't apply `.gitignore` by default; exclude backups when grepping, e.g. `--glob '!backup_before_*'`.

## Data conventions

- `scanner/universal.py` derives identity/revision from filenames: trailing `_A` or `_NN`; `SP-`/`AS-` prefixes and `-GA-` infix set drawing type; a single-digit revision such as `_3` is zero-padded and flagged `NONSTANDARD_FILENAME_REVISION`.
- `config/settings.json` is machine-local user state, rewritten on every button press. Don't hardcode its paths elsewhere and don't treat it as fixtures.
- Version strings are scattered (`PLAN_VERSION`, `WRITER_VERSION`, `PDF_PUBLISHER_VERSION`, `RUNTIME_VERSION`, LSP banners) and version numbers in `README.md`/`PLAN.md` drift from the code. Trust the code and those constants; update the docs only when asked.
