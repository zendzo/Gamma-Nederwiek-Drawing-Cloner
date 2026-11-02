"""One batching strategy for every stage that opens drawings.

Measured on the production machine (AutoCAD 2027 Core Console):

* ``accoreconsole.exe`` startup is ~4.5 s per process;
* one drawing's work (scan, write, validate, plot) is 0.5-2 s.

So a per-drawing process spends most of its life booting. With ``N`` items,
``W`` workers and ``B`` items per process the wall clock is roughly::

    wall ~= N*c + ceil(N / (W * B)) * S

``W`` (the workers spinbox) hides the per-item cost and ``B`` (the batch
spinbox) amortises the startup cost. The title-block discovery stage proved
this on real drawings; every other stage uses the same two levers through this
module so the behaviour (and the progress/cancel contract) cannot drift.

Contract, identical to :mod:`runtime.parallel`:

* ``on_result`` fires in *completion* order - never assume it is ordered;
* the returned list is re-sorted into input order;
* ``should_cancel`` is checked between batches; an in-flight batch finishes;
* an exception inside ``on_result`` or inside the worker function propagates.
"""

from __future__ import annotations

from runtime.parallel import run_pool

__all__ = ["batches", "clamp_batch_size", "run_batched"]

DEFAULT_BATCH_SIZE = 4
MAX_BATCH_SIZE = 16


def clamp_batch_size(value, default=DEFAULT_BATCH_SIZE, maximum=MAX_BATCH_SIZE):
    """Clamp a UI/settings batch size, falling back to *default* when unset.

    ``None`` means "not configured" and keeps the measured default; an explicit
    ``0``/negative is honoured as "one item per process" so a caller can force
    the deterministic serial path.
    """
    if value is None:
        return max(1, int(default))
    try:
        return min(max(1, int(value)), max(1, int(maximum)))
    except (TypeError, ValueError):
        return max(1, int(default))


def batches(items, size=1):
    """Split *items* into fixed-size chunks (the discovery rule).

    ``batch_size <= 1`` yields one chunk per item, i.e. the historical
    one-process-per-drawing path the tests rely on.
    """
    items = list(items)
    step = max(1, int(size or 1))
    return [items[index:index + step] for index in range(0, len(items), step)]


def run_batched(item_function, batch_function, items, batch_size=1, workers=1,
                on_result=None, should_cancel=None):
    """Apply *batch_function* to chunks of *items* through a bounded pool.

    ``batch_size <= 1`` (or fewer than two items) keeps exactly the historical
    serial/per-item pool: ``item_function`` runs once per item. Otherwise each
    worker call handles one whole batch, which is where Core Console startup is
    amortised, and ``batch_function`` must return one result per item.
    """
    items = list(items)
    if not items:
        return []
    if batch_size and int(batch_size) > 1 and len(items) > 1:
        chunks = batches(items, batch_size)

        def work(chunk):
            return list(batch_function(chunk))

        def deliver(chunk_results):
            if on_result:
                for result in chunk_results:
                    on_result(result)

        return [
            result
            for chunk_results in run_pool(work, chunks, workers, deliver, should_cancel)
            for result in chunk_results
        ]
    return run_pool(item_function, items, workers, on_result, should_cancel)
