"""Opt-in script entry point for a local PyCharm Python debug configuration."""
import os
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: record.py SCRIPT [ARG ...]")
    script = Path(sys.argv[1]).resolve(strict=True)
    recorder = None
    if sys.implementation.name == "cpython" and (3, 12) <= sys.version_info[:2] <= (3, 15):
        import argument_origin
        try:
            argument_origin.start(os.environ.get("ARG_ORIGIN_ROOT", script.parent))
            recorder = argument_origin
        except (ValueError, RuntimeError) as error:
            print(f"Argument Origin: {error}. Continuing without recording.", file=sys.stderr)
    else:
        print("Argument Origin requires CPython 3.12–3.15. Continuing without recording.", file=sys.stderr)
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path[0] = str(script.parent)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        if recorder is not None:
            recorder.stop()
