import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = os.environ.get("ARG_ORIGIN_TEST_HELPER", str(ROOT / "python"))
sys.path.insert(0, HELPER)
import argument_origin as origin

FIXTURES = ROOT / "tests/fixtures"


def program(source, **values):
    context = dict(sys=sys, origin=origin, asyncio=asyncio, threading=threading, **values)
    exec(compile(textwrap.dedent(source), str(FIXTURES / "case.py"), "exec"), context)
    return context


def edit_local(frame, name, value):
    frame.f_locals[name] = value
    if sys.version_info[:2] == (3, 12):
        # CPython 3.12 debugger adapters synchronize the locals dictionary.
        import ctypes
        ctypes.pythonapi.PyFrame_LocalsToFast(ctypes.py_object(frame), ctypes.c_int(0))


class RecorderTests(unittest.TestCase):
    def test_recording_launcher_preserves_script_arguments_and_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "scripts").mkdir()
            target = root / "scripts" / "print receipt.py"
            target.with_name("sibling.py").write_text("value = 42\n")
            target.write_text("import json, os, sibling, sys\n"
                              "print(json.dumps([sys.argv, os.getcwd(), sibling.value, __name__]))\n")
            result = subprocess.run([sys.executable, str(Path(HELPER) / "record.py"), str(target),
                                     "two words", "", "--sample"], cwd=root,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout),
                             [[str(target), "two words", "", "--sample"], str(root), 42, "__main__"])

    def record(self, **options):
        recorder = origin.start(FIXTURES, **options)
        self.addCleanup(origin.stop)
        return recorder

    def test_actual_keyword_renames_and_transformation(self):
        scope = program("""
            def entry():
                raw = " a "
                return clean(raw)
            def clean(incoming):
                cooked = incoming.strip()
                return sink(payload=cooked)
            def sink(payload):
                return origin._recording.query(id(sys._getframe()), "payload", "trace")
        """)
        self.record()
        rows = scope["entry"]()["rows"]
        self.assertEqual([r["label"].split(" · ")[0] for r in rows],
                         ["observed", "observed", "derived", "observed"])
        self.assertEqual([r["action"] for r in rows],
                         ["Assigned local", "Received argument", "Assigned local", "Received argument"])
        self.assertTrue(all(r["previous"] is None for r in rows))
        self.assertNotIn("<unbound>", json.dumps(rows))
        self.assertIn("cooked → payload", rows[-1]["detail"])
        self.assertEqual(rows[-1]["call"]["line"], 7)
        self.assertIn("' a '", rows[0]["detail"])
        self.assertIn("'a'", rows[-1]["detail"])

    def test_thousand_distinct_caller_frames(self):
        depth = 1000
        lines = ['def entry():', '    original = "value"', '    return f0(original)']
        for n in range(depth):
            lines += [f"def f{n}(arg{n}):", f"    return f{n+1}(arg{n})"]
        lines += [f"def f{depth}(last):", "    return origin._recording.query(id(sys._getframe()), 'last', 'trace')"]
        scope = program("\n".join(lines))
        previous = sys.getrecursionlimit()
        self.addCleanup(sys.setrecursionlimit, previous)
        sys.setrecursionlimit(4000)
        self.record()
        rows = scope["entry"]()["rows"]
        self.assertEqual(len(rows), depth+2)
        self.assertIn("entry.original", rows[0]["label"])
        self.assertIn("arg999 → last", rows[-1]["detail"])
        self.assertTrue(all(row["label"].startswith("observed") for row in rows))

    def test_equal_unrelated_interned_values_and_shadowed_scopes(self):
        scope = program("""
            def entry(flag):
                left = "same"
                right = "same"
                if flag:
                    chosen = left
                else:
                    chosen = right
                return shadow(chosen)
            def shadow(left):
                return origin._recording.query(id(sys._getframe()), "left", "trace")
        """)
        self.record()
        for flag, expected in [(True, "left"), (False, "right")]:
            rows = scope["entry"](flag)["rows"]
            self.assertIn("entry."+expected, rows[0]["label"])
            self.assertIn("shadow.left", rows[-1]["label"])
            self.assertEqual(len(rows), 3)

    def test_conditional_expression_does_not_guess(self):
        scope = program("""
            def branch(flag, a, b):
                x = a if flag else b
                return origin._recording.query(id(sys._getframe()), "x", "trace")
        """)
        self.record()
        for flag in (False, True):
            rows = scope["branch"](flag, "equal", "equal")["rows"]
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["label"].startswith("unknown"))

    def test_same_line_stores_and_repeated_value_change(self):
        scope = program("""
            def change(value):
                origin._recording.query(id(sys._getframe()), "value", "watch")
                value = "".join(("s", "ame"))
                value = "new"; value = "last"
                return origin._recording.query(id(sys._getframe()), "value", "history")
        """)
        hits = []
        self.record(on_break=lambda f, e: hits.append((e["old"], e["new"], f.f_locals["value"])))
        rows = scope["change"]("same")["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(hits, [("'same'", "'new'", "new"), ("'new'", "'last'", "last")])
        self.assertEqual(rows[-1]["location"], rows[-2]["location"])

    def test_display_distinguishes_none_deletion_and_new_binding(self):
        scope = program("""
            def edit(payload):
                del payload
                payload = "<unbound>"
                payload = "new"
                return origin._recording.query(id(sys._getframe()), "payload", "history")
        """)
        self.record()
        rows = scope["edit"](None)["rows"]
        self.assertEqual([(r["action"], r["previous"], r["value"]) for r in rows], [
            ("Received argument", None, "None"),
            ("Deleted binding", "None", None),
            ("Assigned local", None, "'<unbound>'"),
            ("Reassigned local", "'<unbound>'", "'new'"),
        ])

    def test_async_suspension_and_concurrent_tasks(self):
        scope = program("""
            async def worker(payload):
                await asyncio.sleep(0)
                payload = payload.strip()
                await asyncio.sleep(0)
                return origin._recording.query(id(sys._getframe()), "payload", "history")
            async def entry():
                return await asyncio.gather(worker(" a "), worker(" b "))
        """)
        recorder = self.record()
        histories = asyncio.run(scope["entry"]())
        self.assertEqual([len(h["rows"]) for h in histories], [2, 2])
        args = [e for e in recorder.events.values() if e["function"] == "worker" and e["old"] == "<unbound>"]
        self.assertEqual(len({e["task"] for e in args}), 2)
        self.assertEqual(len({e["frame"] for e in args}), 2)
        self.assertTrue(all(e["edge"] == "unknown" for e in args))
        self.assertEqual(recorder.frames, {})

    def test_two_threads_keep_separate_frames(self):
        scope = program("""
            def worker(payload):
                barrier.wait(timeout=3)
                payload = payload.upper()
                return origin._recording.query(id(sys._getframe()), "payload", "history")
        """, barrier=threading.Barrier(2))
        recorder = self.record()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(scope["worker"], ("a", "b")))
        self.assertTrue(all(len(h["rows"]) == 2 for h in results))
        self.assertEqual(len({e["thread"] for e in recorder.events.values()}), 2)
        self.assertEqual(recorder.frames, {})

    def test_recording_starts_too_late(self):
        def start():
            self.record()
        scope = program("""
            def existing(payload):
                start()
                return origin._recording.query(id(sys._getframe()), "payload", "trace")
        """, start=start)
        report = scope["existing"]("x")
        self.assertIn("Unknown", report["note"])
        self.assertFalse(report["rows"])

    def test_native_callback_and_return_value_boundaries(self):
        scope = program("""
            def key(payload):
                reports.append(origin._recording.query(id(sys._getframe()), "payload", "trace"))
                return payload
            def entry():
                value = "same"
                sorted([value], key=key)
                result = "".join([value])
                return origin._recording.query(id(sys._getframe()), "result", "trace")
        """, reports=[])
        self.record()
        report = scope["entry"]()
        for result in [scope["reports"][0], report]:
            self.assertTrue(result["rows"][0]["label"].startswith("unknown"))
            self.assertEqual(len(result["rows"]), 1)

    def test_fresh_subprocess_is_outside_recording(self):
        self.record()
        out = subprocess.check_output([sys.executable, "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);import argument_origin as a;print(a._recording)",
            HELPER], text=True)
        self.assertEqual(out.strip(), "None")

    @unittest.skipUnless(hasattr(os, "fork"), "Unix only")
    def test_fork_cannot_report_parent_frame(self):
        scope = program("""
            def child_check(payload):
                pid = os.fork()
                if pid == 0:
                    report = origin._recording.query(id(sys._getframe()), "payload", "trace")
                    os._exit(int(bool(report["rows"])))
                return os.waitpid(pid, 0)[1]
        """, os=os)
        self.record()
        self.assertEqual(scope["child_check"]("value"), 0)

    def test_user_repr_equality_and_str_methods_never_imply_producer(self):
        class Trap:
            def __eq__(self, other):
                raise AssertionError("Application equality ran")
            def __repr__(self):
                raise AssertionError("Application repr ran")
            def strip(self):
                return "separate"
        scope = program("""
            def test(x):
                origin._recording.query(id(sys._getframe()), "x", "watch")
                other = x.strip()
                x = replacement
                return origin._recording.query(id(sys._getframe()), "other", "trace")
        """, replacement=Trap())
        hits = []
        self.record(on_break=lambda f, e: hits.append(e["old"]))
        rows = scope["test"](Trap())["rows"]
        self.assertEqual(hits, ["<opaque>"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["label"].startswith("unknown"))

    def test_redaction_follows_renames(self):
        scope = program("""
            def entry():
                api_token = "private-string"
                return sink(api_token)
            def sink(payload):
                clean = payload.strip()
                return origin._recording.query(id(sys._getframe()), "clean", "trace")
        """)
        recorder = self.record()
        report = scope["entry"]()
        self.assertEqual(len(report["rows"]), 3)
        self.assertNotIn("private-string", json.dumps(report))
        self.assertTrue(all(e["new"] == "<redacted>" for e in recorder.events.values()))

    def test_retention_boundary_is_explicit(self):
        scope = program("""
            def entry():
                a = "x"
                b = a
                c = b
                d = c
                return origin._recording.query(id(sys._getframe()), "d", "trace")
        """)
        recorder = self.record(capacity=2)
        report = scope["entry"]()
        self.assertEqual(len(report["rows"]), 2)
        self.assertIn("expired", report["note"])
        self.assertEqual(len(recorder.events), 2)

    def test_closure_write_and_unpack_are_unknown(self):
        scope = program("""
            def entry(payload):
                def write():
                    nonlocal payload
                    payload = "changed"
                write()
                return origin._recording.query(id(sys._getframe()), "payload", "history")
            def unpack(entry):
                a, b = entry
                return origin._recording.query(id(sys._getframe()), "a", "trace")
        """)
        self.record()
        self.assertIn("Unknown", scope["entry"]("start")["note"])
        self.assertTrue(scope["unpack"](("x", "y"))["rows"][0]["label"].startswith("unknown"))

    def test_return_path_through_two_calls(self):
        scope = program("""
            def entry():
                raw = " mixed "
                normalized = relay(raw)
                return origin._recording.query(id(sys._getframe()), "normalized", "trace")
            def relay(forwarded):
                return trim(forwarded)
            def trim(text):
                return text.strip()
        """)
        self.record()
        rows = scope["entry"]()["rows"]
        self.assertEqual([r["location"]["line"] for r in rows], [3, 6, 8, 9, 7, 4])
        self.assertIn("text.strip()", rows[3]["detail"])
        self.assertIn("return from relay", rows[-1]["detail"])

    def test_kwargs_cannot_steal_a_positional_only_origin(self):
        scope = program("""
            def caller():
                original = "left"
                other = "right"
                return sink(original, name=other, options=other)
            def sink(name, /, **options):
                r = origin._recording
                fid = id(sys._getframe())
                return r.query(fid, "name", "trace"), r.query(fid, "options", "trace")
        """)
        self.record()
        pos, extras = scope["caller"]()
        self.assertIn("original → name", pos["rows"][-1]["detail"])
        self.assertTrue(extras["rows"][0]["label"].startswith("unknown"))
        self.assertEqual(len(extras["rows"]), 1)

    def test_change_detects_difference_after_preview_limit(self):
        scope = program("""
            def change(value):
                origin._recording.query(id(sys._getframe()), "value", "watch")
                value = value[:-1] + "b"
        """)
        hits = []
        self.record(on_break=lambda f, e: hits.append(e))
        scope["change"]("a" * 120)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["old"], hits[0]["new"])
        self.assertIn("…", hits[0]["new"])

    def test_busy_monitoring_slots_are_preserved(self):
        mon = sys.monitoring
        for slot in (3, 4):
            mon.use_tool_id(slot, f"existing-{slot}")
            self.addCleanup(mon.free_tool_id, slot)
        with self.assertRaisesRegex(RuntimeError, "No free"):
            origin.start(FIXTURES)
        self.assertIsNone(origin._recording)
        self.assertEqual([mon.get_tool(i) for i in (3, 4)], ["existing-3", "existing-4"])

    def test_external_write_never_creates_a_link(self):
        scope = program("""
            def entry():
                payload = "original"
                edit(sys._getframe(), "payload", "external")
                history = origin._recording.query(id(sys._getframe()), "payload", "trace")
                return history, sink(payload)
            def sink(incoming):
                return origin._recording.query(id(sys._getframe()), "incoming", "trace")
        """, edit=edit_local)
        self.record()
        history, trace = scope["entry"]()
        self.assertFalse(history["rows"])
        self.assertIn("differs", history["note"])
        self.assertEqual(len(trace["rows"]), 1)
        self.assertTrue(trace["rows"][0]["label"].startswith("unknown"))

    def test_redact_all_values(self):
        from unittest.mock import patch
        scope = program("""
            def derive(value):
                edit(sys._getframe(), "x", "private text before assignment")
                x = value.strip()
                return origin._recording.query(id(sys._getframe()), "x", "trace")
        """, edit=edit_local)
        with patch.dict(os.environ, {"ARG_ORIGIN_VALUES": "redacted"}):
            self.record()
        report = scope["derive"]("private text")
        self.assertNotIn("private text", json.dumps(report))
        self.assertIn("<redacted>", json.dumps(report))

    def test_generator_disposal_releases_activation(self):
        import gc
        scope = program("""
            def generator(payload):
                yield payload
            def entry():
                task = generator("x")
                next(task)
        """)
        recorder = self.record()
        scope["entry"]()
        gc.collect()
        self.assertFalse(recorder.frames)

    def test_unpack_default_and_starred_call_are_unattributed(self):
        scope = program("""
            def callee(payload="same"):
                return origin._recording.query(id(sys._getframe()), "payload", "trace")
            def caller():
                source = "same"
                return callee(), callee(*[source])
        """)
        self.record()
        for report in scope["caller"]():
            self.assertEqual(len(report["rows"]), 1)
            self.assertTrue(report["rows"][0]["label"].startswith("unknown"))

    def test_unwind_and_stop_leave_other_monitoring_tool_alone(self):
        scope = program("""
            def failure():
                x = 1
                raise ValueError(x)
        """)
        mon = sys.monitoring
        mon.use_tool_id(3, "existing debugger test")
        self.addCleanup(mon.free_tool_id, 3)
        mon.set_events(3, mon.events.PY_START)
        self.addCleanup(mon.set_events, 3, 0)
        tracer = sys.gettrace()
        recorder = self.record()
        with self.assertRaises(ValueError):
            scope["failure"]()
        self.assertFalse(recorder.frames)
        origin.stop()
        self.assertIs(sys.gettrace(), tracer)
        self.assertEqual(mon.get_events(3), mon.events.PY_START)
        self.assertEqual(mon.get_tool(3), "existing debugger test")
        self.assertIsNone(mon.get_tool(4))


if __name__ == "__main__":
    unittest.main()
