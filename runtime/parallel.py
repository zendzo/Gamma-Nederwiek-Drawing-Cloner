"""Bounded parallel runner shared by the dry-run and publish engines.

``concurrent.futures`` pools are used rather than ad-hoc threads so ``workers=1``
stays exactly serial (the deterministic path the tests rely on). Three contract
points match the writer/validator/PDF engines and must be kept:

* ``on_result`` fires in *completion* order — never assume it is ordered.
* the returned list is re-sorted into input order.
* an exception raised inside ``on_result`` or inside ``function`` propagates to
  the caller instead of leaving the pool's ``wait()`` loop spinning forever.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

__all__ = ["run_pool"]


def run_pool(function, items, workers=1, on_result=None, should_cancel=None):
    """Apply *function* to every item and return results in input order."""
    items = list(items)
    if not items:
        return []
    workers = max(1, int(workers or 1))
    if workers == 1:
        results = []
        for item in items:
            if should_cancel and should_cancel():
                break
            result = function(item)
            results.append(result)
            if on_result:
                on_result(result)
        return results

    def call(index, item):
        # (index, result) maps a result to its input without relying on identity
        # or on completion order, including when a cancel truncates the run.
        return index, function(item)

    slots = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {
            pool.submit(call, index, item): index for index, item in enumerate(items)
        }
        while pending:
            stopping = should_cancel is not None and should_cancel()
            done, _ = wait(list(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            if stopping:
                for future in list(pending):
                    if future.cancel():
                        pending.pop(future)
                if not done:
                    break
            for future in list(done):
                pending.pop(future, None)
                if future.cancelled():
                    continue
                index, result = future.result()
                slots[index] = result
                if on_result:
                    on_result(result)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    return [slots[index] for index in sorted(slots)]
