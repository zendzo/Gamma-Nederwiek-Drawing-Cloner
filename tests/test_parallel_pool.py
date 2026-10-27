import threading
import time
import unittest

from runtime.parallel import run_pool


class RunPoolTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(run_pool(str, []), [])

    def test_serial_path_preserves_order(self):
        self.assertEqual(run_pool(lambda x: x * 2, [1, 2, 3], workers=1), [2, 4, 6])

    def test_serial_path_still_reports_results(self):
        seen = []
        run_pool(lambda x: x + 1, [1, 2], workers=1, on_result=seen.append)
        self.assertEqual(seen, [2, 3])

    def test_parallel_results_are_returned_in_input_order(self):
        def slow(value):
            # Later items finish first; the output must still be input order.
            time.sleep(0.05 * (4 - value))
            return value

        self.assertEqual(run_pool(slow, [0, 1, 2, 3], workers=4), [0, 1, 2, 3])

    def test_parallel_matches_serial_output(self):
        function = lambda x: x * x
        items = list(range(20))
        self.assertEqual(
            run_pool(function, items, workers=1),
            run_pool(function, items, workers=8),
        )

    def test_on_result_fires_for_every_item_in_completion_order(self):
        seen = []
        lock = threading.Lock()

        def record(result):
            with lock:
                seen.append(result)

        run_pool(lambda x: x, list(range(10)), workers=4, on_result=record)
        self.assertEqual(sorted(seen), list(range(10)))

    def test_cancel_stops_early_and_truncates_results(self):
        calls = []
        flag = {"stop": False}

        def work(value):
            calls.append(value)
            if value == 2:
                flag["stop"] = True
            return value

        results = run_pool(work, list(range(50)), workers=1, should_cancel=lambda: flag["stop"])
        self.assertLess(len(results), 50)
        self.assertTrue(results)

    def test_cancel_before_start_returns_nothing(self):
        self.assertEqual(
            run_pool(lambda x: x, [1, 2, 3], workers=1, should_cancel=lambda: True), []
        )

    def test_on_result_exception_propagates_instead_of_hanging(self):
        # The pre-P0 bug class: a raising callback wedged the pool's wait() loop.
        def explode(result):
            raise ValueError("callback failed")

        with self.assertRaises(ValueError):
            run_pool(lambda x: x, [1, 2, 3, 4], workers=2, on_result=explode)

    def test_function_exception_propagates(self):
        def boom(value):
            raise RuntimeError("worker failed")

        with self.assertRaises(RuntimeError):
            run_pool(boom, [1, 2], workers=2)

    def test_workers_is_clamped(self):
        self.assertEqual(run_pool(lambda x: x, [1, 2], workers=0), [1, 2])
        self.assertEqual(run_pool(lambda x: x, [1, 2], workers=-5), [1, 2])


if __name__ == "__main__":
    unittest.main()
