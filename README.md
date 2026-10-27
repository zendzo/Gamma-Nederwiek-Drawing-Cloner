# Gamma → Nederwiek Revision Migration (0.4C.6)

Windows desktop app (PySide6) that migrates Gamma AutoCAD drawing bundles into the Nederwiek destination structure. It drives `accoreconsole.exe` (AutoCAD Core Console) with the AutoLISP scripts in `lisp/`; source drawings are never modified.

## What it does

- Scans source folders for DWG, XLS/XLSX, NC1 and DXF, and derives drawing identity/revision from file names.
- Builds the canonical `<destination>/<bundle>/{A,00}/...` tree and copies both embedded raster assets into every CAD folder.
- Clones supporting files (Part List, NC1, DXF) with a selectable collision policy.
- Discovers title-block fields and builds a spatial revision plan for the A and 00 outputs.
- Writes A/00 drawings in Core Console (QSAVE only), validates them by reopening, publishes atomically with backups and journals, then plots PDFs.
- Every stage writes a report/checkpoint under `_GNI_REPORTS`; those files are also the contract between stages and the resume mechanism.

## Workflow (the UI)

The Setup tab is a pipeline, not a wall of buttons. It shows one table of the ten stages and one primary button that always names the next real action:

| Stage | Report it produces |
| --- | --- |
| Scan source | (in-session candidate groups) |
| Build destination | `destination_manifest.json` |
| Clone supporting files | `supporting_file_clone_report.json` |
| Discover title blocks | `title_block_discovery_fields.csv` |
| Build revision plan | `revision_pilot_write_plan.json` |
| Stable external writer | `stable_external_writer_report.json` |
| Reopen validator | `reopen_validation_report.json` |
| Atomic publish dry run | `atomic_publish_dry_run_report.json` |
| Atomic publisher | `atomic_publish_report.json` |
| Publish PDFs | `pdf_publish_report.json` |

1. Fill in Source, Destination root, Bundle name and `accoreconsole.exe` (saved to `config/settings.json`).
2. Press the primary button once. It runs every remaining non-destructive stage in order and stops at the publish gate: `Run migration to publish gate: <next stage>`.
3. Review the dry-run report in the Reports tab, then press `Publish N validated pair(s)`. The two publishing stages are never chained automatically.
4. Press `Publish PDFs` afterwards; it only plots pairs published by the atomic publisher.

Supporting behaviours:

- Completed stages are skipped. A stage whose report is older than its predecessor is shown as `STALE` and is re-run automatically, so changing an early input does not silently reuse downstream results.
- `Cancel Safely` stops after the current drawing/plot; finished work is kept through the checkpoints.
- `Parallel workers` (1–16, saved in settings) sets how many Core Console processes run at once for the writer, validator and PDF stages. 4–6 is a good default; each process needs roughly 1–2 GB of RAM.
- `Advanced - run or re-run one stage` (collapsed) exposes the ten individual stages for recovery or experimentation.
- The Reports tab lists every `_GNI_REPORTS` file; double-click opens it.

## Requirements

- Windows and Python 3.10+ (the launcher uses 3.12).
- AutoCAD with `accoreconsole.exe` (verified with AutoCAD 2027; 2018+ expected).
- `resources/reference_images/SEAtrium_Ge_.PNG` and `resources/reference_images/Tennet LogO New.PNG`.

## Run

1. `run_app.bat` installs `uv` when needed, creates `.venv`, installs `requirements.txt`, then starts the app. Manual start: `.\.venv\Scripts\python.exe app.py`.
2. Set the paths above; they are saved to `config/settings.json`.
3. Use the primary button as described in **Workflow**. A stage refuses to start when its upstream report is missing.

## Throughput

Core Console startup costs about 5.5 s, so one process per drawing is dominated by startup, not by the edit or plot. The 0.4C.6 writer, validator and PDF publisher therefore run several independent drawings concurrently (`ThreadPoolExecutor`), and both the writer and the PDF publisher skip work that provably has not changed.

Measured on the three-drawing TEST-004 bundle (6 A/00 DWGs, 24 logical CPUs):

| Stage | 1 worker | 4 workers | Rerun with nothing changed |
| --- | --- | --- | --- |
| Stable external writer | 24.9 s | 11.3 s (2.2×) | reused from checkpoint |
| PDF publisher (cold) | 30.8 s | 19.3 s (1.6×, includes a 2-plot preflight) | 0.02 s (all skipped) |
| PDF publisher (steady state, no preflight) | — | 15.2 s | — |

Scaling is per drawing pair, so a 100-pair bundle at 4 workers is roughly 25 batches of ~15 s instead of 100 sequential pairs. The PDF preflight (first pair plotted alone to prove plotter/media are usable) is paid once per configuration change, never on ordinary reruns.

## Output

- Drawings and PDFs: `<destination>/<bundle>/{A,00}/{1. SP,2. AS,3. GA}/{CAD,PDF}`
- Supporting files: `00/4. PART LIST` and `00/5. NC1 & DXF`
- Reports and checkpoints: `<bundle>/_GNI_REPORTS` (shown in the in-app Reports tab; double-click to open)
- Transaction artifacts: `<bundle>/_GNI_STAGING` and `<bundle>/_GNI_BACKUP`
- Core Console jobs and logs: `%TEMP%\GNI\...`

## Tests

```bat
.\.venv\Scripts\python.exe -m unittest discover -t . -s tests -v
```

The suite uses the standard-library `unittest` runner and does not need AutoCAD.

## Current limitations

- Revision clouds / obsolete entities are not deleted; only detected revision-table rows are cleared.
- DXF files are copied, not content-migrated.
- PDF plotting uses single-page model space plus the media names in `revision/pdf_settings.json`. Paper names must match the plotter exactly (for example `ISO A1 (841.00 x 594.00 MM)`, no underscores).
- Two revisions of the same drawing are plotted sequentially inside one pair; concurrency is across pairs.

See `PLAN.md` for the roadmap and `AGENTS.md` for development notes.
