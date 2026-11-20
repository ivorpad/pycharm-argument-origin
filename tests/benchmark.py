"""Median elapsed time on the same CPU-bound script with each capture mode."""
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import argument_origin as origin


def workload(iterations):
    result = 0
    for step in range(iterations):
        result = (result + step) % 97
    return result


def measure():
    workload(100)
    elapsed = []
    for _ in range(7):
        begin = perf_counter_ns()
        answer = workload(5000)
        elapsed.append((perf_counter_ns() - begin) / 1e6)
        assert answer == sum(range(5000)) % 97
    return median(elapsed)


if __name__ == "__main__":
    print(f"CPython {platform.python_version()}, {platform.machine()}, median of 7 runs, 5000 iterations")
    baseline = measure()
    print(f"Recording off       {baseline:9.3f} ms")
    for label, root in [("Outside file filter", ROOT / "example"), ("Recorded loop", Path(__file__).parent)]:
        recorder = origin.start(root)
        try:
            value = measure()
            print(f"{label:19} {value:9.3f} ms  {value / baseline:7.1f}x  {len(recorder.events)} retained")
        finally:
            origin.stop()
