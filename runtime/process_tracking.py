"""Shared helpers for Console-Console child processes and temp job folders.

`accoreconsole.exe` runs are long lived (startup alone is ~5 s). When the GUI is
closed mid-run, nothing currently stops those children: they keep writing to
``%TEMP%\\GNI`` and can hold ``.dwg``/``.dwl`` locks on files the next run wants
to touch. This module keeps a registry of live children so the window's
``closeEvent`` can terminate them deterministically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Lock

__all__ = [
    "child_registry",
    "gni_temp_root",
    "prune_job_root",
    "run_console",
]


class ChildRegistry:
    """Tracks live subprocess children so they can be terminated on shutdown."""

    def __init__(self):
        self._lock = Lock()
        self._children = {}
        self._shutting_down = False

    def register(self, process):
        with self._lock:
            self._children[id(process)] = process
            shutting = self._shutting_down
        if shutting:
            # Shutdown started while this process was being spawned; do not let
            # it outlive the application.
            self._terminate(process)
        return process

    def unregister(self, process):
        with self._lock:
            self._children.pop(id(process), None)

    @property
    def active(self):
        with self._lock:
            return len(self._children)

    def begin_shutdown(self):
        with self._lock:
            self._shutting_down = True

    def cancel_shutdown(self):
        with self._lock:
            self._shutting_down = False

    def terminate_all(self, grace=3.0):
        """Terminate every live child, then kill whatever is still alive."""
        with self._lock:
            children = list(self._children.values())
        for process in children:
            self._terminate(process, gentle=True)
        deadline = time.monotonic() + grace
        for process in children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining or 0.1)
            except subprocess.TimeoutExpired:
                self._terminate(process, gentle=False)
        return len(children)

    @staticmethod
    def _terminate(process, gentle=True):
        if process.poll() is not None:
            return
        try:
            if gentle:
                process.terminate()
            else:
                process.kill()
        except OSError:
            pass


child_registry = ChildRegistry()


def run_console(command, **kwargs):
    """Drop-in `subprocess.run` replacement that registers the child.

    Accepts the same keyword arguments as :func:`subprocess.run`. On timeout the
    child is killed and re-raised, matching the previous behaviour callers rely
    on (they all catch ``subprocess.TimeoutExpired``).
    """
    timeout = kwargs.pop("timeout", None)
    # subprocess.run-only keywords; Popen takes the underlying pipe streams instead.
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = subprocess.Popen(command, **kwargs)
    child_registry.register(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        child_registry._terminate(process, gentle=False)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    finally:
        child_registry.unregister(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def gni_temp_root():
    return Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve() / "GNI"


def prune_job_root(root, keep_days=7, now=None):
    """Remove job folders under *root* older than ``keep_days``.

    ``trusted_runtime`` is never touched: the LISP copies there are rewritten
    on every engine start, but deleting them mid-run would break a live worker.
    Returns ``(removed, failed)`` counts; never raises.
    """
    root = Path(root)
    if not root.is_dir():
        return 0, 0
    cutoff = (now or time.time()) - keep_days * 86400
    removed = failed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name == "trusted_runtime":
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(child, ignore_errors=False)
            removed += 1
        except OSError:
            failed += 1
    return removed, failed
