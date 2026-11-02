"""Real-Core-Console benchmark: one process per drawing vs. batched.

Run manually on the Windows workstation (it needs a licensed
``accoreconsole.exe``), never as part of the offline suite:

    .\\.venv\\Scripts\\python.exe tests\\bench_discovery_batch.py

It discovers the same N drawings twice — ``batch=1`` (the old behaviour) and
``batch=4`` — and prints the measured wall clock so the claim "startup
dominates" can be re-checked on the machine actually doing the work.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from titleblock.discovery import TitleBlockDiscoveryEngine


def _inputs():
    listing = Path(tempfile.gettempdir()) / "gni_bench_inputs.txt"
    if listing.is_file():
        paths = [Path(line.strip()) for line in listing.read_text().splitlines() if line.strip()]
    else:
        # Fall back to the staged copies of previous runs.
        root = Path(tempfile.gettempdir()) / "GNI" / "titleblock_discovery"
        paths = sorted(root.glob("*/input.dwg"))[-8:]
    return [path for path in paths if path.is_file()]


def _groups(paths):
    return [
        SimpleNamespace(identity=f"BENCH{index:02d}", drawing_type="GA", primary_source=str(path))
        for index, path in enumerate(paths)
    ]


def main():
    setting = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    core = setting["core_console"]
    if not Path(core).is_file():
        print(f"accoreconsole not found: {core}")
        return 2
    paths = _inputs()
    if not paths:
        print("No benchmark DWGs found.")
        return 2
    groups = _groups(paths)
    print(f"drawings: {len(groups)}")
    settings = SimpleNamespace(
        project_from=setting.get("project_from", ""), project_to=setting.get("project_to", ""),
        drawing_from=setting.get("drawing_from", ""), drawing_to=setting.get("drawing_to", ""),
        long_from=setting.get("long_from", ""), long_to=setting.get("long_to", ""),
        short_from=setting.get("short_from", ""), short_to=setting.get("short_to", ""),
    )
    lisp = ROOT / "lisp" / "title_block_discovery.lsp"
    results = {}
    for batch in (1, 4):
        engine = TitleBlockDiscoveryEngine(core, lisp, settings, ROOT)
        started = time.monotonic()
        if batch == 1:
            outcomes = [engine.discover_group(group) for group in groups]
        else:
            outcomes = engine.discover_many(groups, batch_size=batch)
        elapsed = time.monotonic() - started
        ok = sum(item.status == "SUCCESS" for item in outcomes)
        fields = sum(len(item.fields) for item in outcomes)
        results[batch] = (elapsed, ok, fields)
        print(f"batch={batch}: {elapsed:6.1f}s  success={ok}/{len(outcomes)}  fields={fields}"
              f"  ({elapsed/max(1,len(outcomes)):.2f}s per drawing)")
        for item in outcomes:
            if item.status != "SUCCESS":
                print(f"   FAILED {item.identity}: {item.error}")
    serial, batched = results[1][0], results[4][0]
    if batched:
        print(f"speed-up at batch=4: {serial/batched:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
