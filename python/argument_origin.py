"""In-process, opt-in binding recorder for CPython and local PyCharm pydevd."""
import asyncio
import collections
import dis
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone

MISSING = object()
UNWATCHED = object()
PRIVATE = re.compile(r"passw|secret|token|credential|api_?key|authorization", re.I)
STRING_METHODS = {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "capitalize"}
HELPER_FILES = {__file__, os.path.join(os.path.dirname(__file__), "record.py")}
_recording = None


def preview(name, value, private=False):
    if value is MISSING:
        return "<unbound>"
    if private or PRIVATE.search(name):
        return "<redacted>"
    if type(value) is str:
        return repr(value[:96]) + ("…" if len(value) > 96 else "")
    if type(value) is bool or value is None:
        return repr(value)
    if type(value) is int and value.bit_length() < 128:
        return str(value)
    if type(value) is float:
        return repr(value)
    return "<opaque>"


def location(code, line):
    return {"path": code.co_filename, "line": line or code.co_firstlineno}


@dataclass
class Store:
    name: str
    line: int
    edge: str
    description: str
    parent: int | None
    old: str
    before: object
    private: bool
    successor: int
    path: tuple


@dataclass
class Activation:
    serial: int
    caller: str
    callsite: dict | None
    latest: dict = field(default_factory=dict)
    object_ids: dict = field(default_factory=dict)
    sensitive: set = field(default_factory=set)
    lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=24))
    pending: list[Store] = field(default_factory=list)
    call: tuple | None = None
    return_site: tuple | None = None
    returned: tuple | None = None
    watch: str | None = None
    watch_file: str | None = None

    def clear_watch(self):
        if self.watch_file:
            _watch_report(self.watch_file, {"closed": True})
        self.watch = self.watch_file = None


FAST_LOADS = {"LOAD_FAST", "LOAD_FAST_BORROW", "LOAD_FAST_CHECK"}
PAIR_LOADS = {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}
CONSTANT_LOADS = {"LOAD_CONST", "LOAD_SMALL_INT"}


def direct_loads(op):
    if op.opname in FAST_LOADS:
        return (("local", op.argval),)
    if op.opname in PAIR_LOADS:
        return tuple(("local", name) for name in op.argval)
    if op.opname in CONSTANT_LOADS:
        return (("literal", None),)
    return ()


def call_spec(ops, j, targets):
    ins = ops[j]
    k = j - 1
    keys = ()
    if ops[k].opname == "KW_NAMES" or ins.opname == "CALL_KW":
        keys = ops[k].argval
        if type(keys) is not tuple:
            return None
        k -= 1
    args = []
    while len(args) < ins.arg and k >= 0:
        loads = direct_loads(ops[k])
        if not loads:
            return None
        args[0:0] = loads
        k -= 1
    if (len(args) != ins.arg or k < 0 or ops[k].opname not in FAST_LOADS | {"LOAD_GLOBAL"}
            or any(op.offset in targets for op in ops[k+1:j+1])):
        return None
    return keys, args, ops[j+1].offset


def producer(ops, j, position, targets):
    ins = ops[j]
    if j == 0 or ins.offset in targets:
        return "unknown", None, None
    prev = ops[j-1]
    inputs = direct_loads(prev)
    if position < len(inputs):
        kind, src = inputs[-1-position]
        return ("observed" if kind == "local" else "literal"), src, None
    if position == 0 and prev.opname in ("CALL", "CALL_KW"):
        if (j >= 3 and prev.arg == 0 and ops[j-2].opname == "LOAD_ATTR"
                and ops[j-2].argval in STRING_METHODS and ops[j-3].opname in FAST_LOADS
                and not targets.intersection((prev.offset, ops[j-2].offset))):
            return "derived", ops[j-3].argval, ops[j-2].argval
        return "return", None, None
    return "unknown", None, None


@dataclass
class CodePlan:
    code: types.CodeType
    stores: dict
    after: set
    calls: dict
    returns: dict


def plan(code):
    ops = list(dis.get_instructions(code))
    targets = {i.offset for i in ops if i.is_jump_target}
    targets.update(e.target for e in dis.Bytecode(code).exception_entries)
    stores, after, calls, returns = {}, set(), {}, {}
    for j, ins in enumerate(ops):
        if ins.opname == "RETURN_VALUE":
            returns[ins.offset] = producer(ops, j, 0, targets)
        elif ins.opname == "RETURN_CONST":
            returns[ins.offset] = ("literal", None, None)
        if ins.opname in ("CALL", "CALL_KW"):
            spec = call_spec(ops, j, targets)
            if spec is not None:
                calls[ins.offset] = spec
        if ins.opname == "STORE_FAST_STORE_FAST":
            names = ins.argval
        elif ins.opname == "STORE_FAST_LOAD_FAST":
            names = ins.argval[:1]
        elif ins.opname in ("STORE_FAST", "DELETE_FAST", "LOAD_FAST_AND_CLEAR"):
            names = (ins.argval,)
        else:
            continue
        items = []
        for position, name in enumerate(dict.fromkeys(names)):
            how, src, method = "unknown", None, None
            if ins.opname.startswith("STORE_FAST") and names.count(name) == 1:
                how, src, method = producer(ops, j, position, targets)
            items.append((name, ins.positions.lineno, how, src, method, ops[j+1].offset))
        stores[ins.offset] = items
        after.add(ops[j+1].offset)
    return CodePlan(code, stores, after, calls, returns)


class Recording:
    def __init__(self, root, capacity, on_break):
        resolved = os.path.realpath(root)
        self.root = resolved + os.sep
        self.file = resolved if os.path.isfile(resolved) else None
        self.hide_values = os.environ.get("ARG_ORIGIN_VALUES") == "redacted"
        self.capacity = capacity
        self.pid = os.getpid()
        self.frames = {}
        self.events = collections.OrderedDict()
        self.serial = itertools.count(1)
        self.ids = itertools.count(1)
        self.on_break = on_break
        self.codes = {}
        self.tool = None
        self.dropped = 0

    def install(self):
        mon = sys.monitoring
        for tool in (3, 4):
            try:
                mon.use_tool_id(tool, "Argument Origin")
            except ValueError:
                continue
            self.tool = tool
            break
        if self.tool is None:
            raise RuntimeError("No free application monitoring tool ID (3 or 4)")
        events = mon.events
        self.callbacks = {events.PY_START: self.enter, events.CALL: self.call,
                          events.INSTRUCTION: self.instruction, events.PY_RETURN: self.returned,
                          events.PY_UNWIND: self.leave, events.LINE: self.line}
        try:
            for event, callback in self.callbacks.items():
                mon.register_callback(self.tool, event, callback)
            mon.set_events(self.tool, events.PY_START | events.PY_UNWIND)
        except BaseException:
            self.close()
            raise

    def close(self):
        mon = sys.monitoring
        mon.set_events(self.tool, 0)
        for program in self.codes.values():
            mon.set_local_events(self.tool, program.code, 0)
        for event in self.callbacks:
            mon.register_callback(self.tool, event, None)
        mon.free_tool_id(self.tool)
        if self.pid == os.getpid():
            for state in self.frames.values():
                state.clear_watch()
        self.frames.clear()
        self.codes.clear()

    def enter(self, code, offset):
        if not code.co_flags & 1 or self.pid != os.getpid():
            return
        if code.co_filename != self.file and not code.co_filename.startswith(self.root):
            return
        if code.co_filename in HELPER_FILES:
            return
        if id(code) not in self.codes:
            if len(self.codes) >= 4096:
                return
            self.codes[id(code)] = plan(code)
            ev = sys.monitoring.events
            sys.monitoring.set_local_events(self.tool, code, ev.INSTRUCTION | ev.CALL | ev.PY_RETURN | ev.LINE)
        frame = sys._getframe(1)
        self.frames.pop(id(frame), None)
        if len(self.frames) >= 8192:
            return
        parent = frame.f_back
        source = self.frames.get(id(parent))
        site = location(parent.f_code, parent.f_lineno) if source else None
        caller = f"{parent.f_code.co_qualname} #{source.serial}" if source else "outside recording"
        state = Activation(next(self.serial), caller, site)
        self.frames[id(frame)] = state
        bindings = {}
        if source and source.call:
            target, call_offset, call_end, bindings_at_call = source.call
            # CALL is delivered before frame entry. Deferred generator/coroutine
            # execution and implicit C callbacks cannot consume a stale call.
            if target is code and call_offset <= parent.f_lasti < call_end and not code.co_flags & 0x2a0:
                bindings = bindings_at_call
                state.return_site = (source.serial, call_offset, call_end)
            source.call = None
        count = code.co_argcount + code.co_kwonlyargcount
        count += bool(code.co_flags & 4) + bool(code.co_flags & 8)
        args = frame.f_locals
        for name in code.co_varnames[:count]:
            if name in code.co_cellvars:
                continue
            edge, parent_id, desc, private = bindings.get(name, ("unknown", None, "caller expression unavailable", False))
            self.add(frame, state, name, edge, desc, parent_id, "<unbound>", args.get(name, MISSING),
                     code.co_firstlineno, private, kind="argument")

    def line(self, code, line):
        frame = sys._getframe(1)
        state = self.frames.get(id(frame))
        if state and self.pid == os.getpid():
            state.lines.append(line)

    def call(self, code, offset, target, first):
        frame = sys._getframe(1)
        state = self.frames.get(id(frame))
        if not state or self.pid != os.getpid():
            return
        state.call = None
        state.returned = None
        if type(target) is not types.FunctionType:
            return
        spec = self.codes[id(code)].calls.get(offset)
        if spec is None:
            return
        keys, args, call_end = spec
        callee = target.__code__
        positional = len(args)-len(keys)
        if positional > callee.co_argcount:
            return
        names = (*callee.co_varnames[:positional], *keys)
        bindings = {}
        keyword_formals = callee.co_varnames[callee.co_posonlyargcount:callee.co_argcount + callee.co_kwonlyargcount]
        for index, (name, (op, arg)) in enumerate(zip(names, args)):
            if index >= positional and name not in keyword_formals:
                continue
            if op == "literal":
                bindings[name] = ("observed", None, "literal argument", False)
            elif arg in state.latest and id(frame.f_locals.get(arg, MISSING)) == state.object_ids[arg]:
                bindings[name] = ("observed", state.latest[arg], f"{arg} → {name}", arg in state.sensitive)
        state.call = (callee, offset, call_end, bindings)

    def add(self, frame, state, name, edge, desc, parent, old, value, line, private, path=(), kind="assignment"):
        private = bool(self.hide_values or private or PRIVATE.search(name))
        if private:
            state.sensitive.add(name)
        else:
            state.sensitive.discard(name)
        task = asyncio.current_task() if _running_loop() else None
        event_id = next(self.ids)
        item = dict(id=event_id, parent=parent, frame=state.serial, function=frame.f_code.co_qualname,
                    name=name, kind=kind, edge=edge, description=desc, old=old, new=preview(name, value, private),
                    location=location(frame.f_code, line), call=state.callsite, caller=state.caller,
                    thread=threading.get_ident(), task=id(task) if task is not None else None, ns=time.time_ns(), path=path)
        self.events[event_id] = item
        state.latest[name] = event_id
        state.object_ids[name] = id(value)
        if len(self.events) > self.capacity:
            self.events.popitem(last=False)
            self.dropped += 1
        return item

    def instruction(self, code, offset):
        program = self.codes[id(code)]
        if offset not in program.stores and offset not in program.after:
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)
        state = self.frames.get(id(frame))
        if not state or self.pid != os.getpid():
            return
        if state.pending:
            self.finish_store(frame, state, offset)
        for name, line, edge, src, method, successor in program.stores.get(offset, ()):
            loc = frame.f_locals
            before = loc.get(name, MISSING)
            edge, parent, desc, private = self.resolve(state, loc, offset, name, edge, src, method)
            state.pending.append(Store(name, line, edge, desc, parent,
                                  preview(name, before, self.hide_values or name in state.sensitive),
                                  before if state.watch == name else UNWATCHED, private,
                                  successor, tuple(state.lines)))

    @staticmethod
    def resolve(state, loc, offset, name, how, src, method):
        if how == "literal":
            return "observed", None, "literal assignment" if name != "<return>" else "literal return", False
        if how == "return" and state.returned and state.returned[0] == offset:
            _, index, func, private = state.returned
            return "observed", index, f"return from {func} → {name}", private
        parent = state.latest.get(src)
        private = src in state.sensitive
        if parent is not None and id(loc.get(src, MISSING)) == state.object_ids[src]:
            if how == "observed":
                return how, parent, f"{src} → {name}", private
            if how == "derived" and type(loc.get(src)) is str:
                return how, parent, f"{src}.{method}() → {name}", private
        return "unknown", None, "producer unavailable", private

    def returned(self, code, offset, value):
        frame = sys._getframe(1)
        state = self.frames.pop(id(frame), None)
        if state and state.watch and self.pid == os.getpid():
            state.clear_watch()
        if not state or not state.return_site or self.pid != os.getpid():
            return
        serial, call_offset, end = state.return_site
        caller = frame.f_back
        parent = self.frames.get(id(caller))
        if not parent or parent.serial != serial or not call_offset <= caller.f_lasti < end:
            return
        caller_plan = self.codes[id(caller.f_code)]
        if end not in caller_plan.stores and (not parent.return_site or end not in caller_plan.returns):
            return
        spec = self.codes[id(code)].returns.get(offset)
        if spec is None:
            return
        edge, previous, desc, private = self.resolve(state, frame.f_locals, offset, "<return>", *spec)
        if edge == "unknown":
            return
        event = self.add(frame, state, "<return>", edge, desc, previous, "<unbound>", value,
                         frame.f_lineno, private, tuple(state.lines), kind="return")
        parent.returned = end, event["id"], code.co_qualname, "<return>" in state.sensitive

    def finish_store(self, frame, state, offset, already_paused=False):
        pending = state.pending
        state.pending = []
        if offset != pending[0].successor:
            return
        hit = None
        for store in pending:
            value = frame.f_locals.get(store.name, MISSING)
            event = self.add(frame, state, store.name, store.edge, store.description, store.parent,
                             store.old, value, store.line, store.private, store.path)
            if state.watch == store.name and store.before is not UNWATCHED and _changed(store.before, value):
                hit = event
        if hit is None:
            return
        if state.watch_file:
            note = f"Changed at line {hit['location']['line']}. Resume to stop at the next change."
            _watch_report(state.watch_file, dict(hit=hit["id"], title=f"Value changed · {hit['name']}",
                                                 note=note, rows=[_row(hit)]))
        if not already_paused:
            self.on_break(frame, hit)

    def leave(self, code, offset, value):
        if id(code) in self.codes:
            state = self.frames.pop(id(sys._getframe(1)), None)
            if state and state.watch and self.pid == os.getpid():
                state.clear_watch()

    def query(self, frame_id, name, mode, watch_file=None):
        state = self.frames.get(frame_id)
        mismatch = False
        if state and self.pid == os.getpid():
            frame = _live_frame(frame_id)
            if frame is not None:
                # A native line breakpoint can precede our post-store instruction.
                if state.pending and frame.f_lasti == state.pending[0].successor:
                    self.finish_store(frame, state, frame.f_lasti, already_paused=True)
                mismatch = name in state.object_ids and id(frame.f_locals.get(name, MISSING)) != state.object_ids[name]
        title = {"watch": "Value-change breakpoint", "unwatch": "Value-change breakpoint",
                 "trace": "Argument origin", "history": "Value history"}[mode]
        report = {"title": f"{title} · {name}", "rows": [], "note": ""}
        if not state or name not in state.latest or self.pid != os.getpid():
            report["note"] = "Unknown. This binding was outside the recording. Rerun with record.py before entering the caller."
            return report
        if mismatch and mode == "trace":
            report["note"] = "Unknown. The current binding differs from the last recorded store."
            return report
        if mode == "unwatch":
            if state.watch == name:
                state.clear_watch()
                report["note"] = f"Stopped watching {name} in frame #{state.serial}."
            else:
                report["note"] = f"No value-change breakpoint on {name} in this frame."
            return report
        if mode == "watch":
            if self.on_break == _suspend and _debugger() is None:
                report["note"] = "Watch unavailable. Use the local PyCharm pydevd debugger."
            else:
                if state.watch_file != watch_file:
                    state.clear_watch()
                state.watch = name
                state.watch_file = watch_file
                report["note"] = f"Armed for frame #{state.serial}. Resume to stop each time {name} changes."
            return report
        if mode == "history":
            rows = [e for e in self.events.copy().values() if e["frame"] == state.serial and e["name"] == name]
            report["note"] = f"Frame #{state.serial} · {len(rows)} retained bindings, in execution order."
            if mismatch:
                report["note"] += " The current binding changed outside the recording."
            if self.dropped:
                report["note"] += " Older events may have expired."
        else:
            rows = []
            index = state.latest[name]
            while index is not None and (event := self.events.get(index)) is not None:
                rows.append(event)
                index = event["parent"]
            rows.reverse()
            report["note"] = "Earliest retained producer first."
            if index is not None:
                report["note"] += " Unknown earlier origin (event expired)."
        report["rows"] = [_row(e) for e in rows]
        return report


def _live_frame(frame_id):
    for frame in sys._current_frames().values():
        while frame:
            if id(frame) == frame_id:
                return frame
            frame = frame.f_back
    return None


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _row(e):
    timestamp = datetime.fromtimestamp(e["ns"]/1e9, timezone.utc).isoformat(timespec="milliseconds")
    lines = " → ".join(map(str, e["path"]))
    path = f"\nRecent lines: {lines}" if lines else ""
    old, new = e["old"], e["new"]
    if e["kind"] == "argument":
        action = "Received argument"
    elif e["kind"] == "return":
        action = "Returned value"
    elif new == "<unbound>":
        action = "Deleted binding"
    elif old == "<unbound>":
        action = "Assigned local"
    else:
        action = "Reassigned local"
    previous = None if old == "<unbound>" else old
    current = None if new == "<unbound>" else new
    values = f"{previous} → {current}" if previous is not None and current is not None else (current or previous or "")
    return dict(name=e["name"], function=e["function"], action=action, value=current, previous=previous,
                forwarded=e["kind"] == "argument" and e["edge"] == "observed" and e["parent"] is not None,
                edge=e["edge"], description=e["description"], frame=e["frame"], caller=e["caller"],
                time=timestamp, thread=str(e["thread"]), task=e["task"], lines=lines,
                label=f'{e["edge"]} · {e["function"]}.{e["name"]}  (#{e["frame"]})',
                detail=f'{e["description"]}\n{values}\n'
                       f'{timestamp} · thread {e["thread"]} · task {e["task"] or "none"}\nfrom {e["caller"]}{path}',
                location=e["location"], call=e["call"])


def _changed(before, after):
    if before is after:
        return False
    if type(before) is type(after) and any(type(before) is t for t in (str, int, bytes, bool, float)):
        return before != after
    return True


def _debugger():
    constants = sys.modules.get("_pydevd_bundle.pydevd_constants")
    return constants.get_global_debugger() if constants and hasattr(constants, "get_global_debugger") else None


def _suspend(frame, event):
    db = _debugger()
    if db is not None:
        from _pydevd_bundle.pydevd_comm_constants import CMD_SET_BREAK
        thread = threading.current_thread()
        db.set_suspend(thread, CMD_SET_BREAK)
        thread.additional_info.pydev_message = f'{event["name"]}: {event["old"]} → {event["new"]}'
        db.do_wait_suspend(thread, frame, "line", None)


def start(root, *, capacity=16384, on_break=None):
    global _recording
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in ((3, 12), (3, 13), (3, 14), (3, 15)):
        raise RuntimeError("Argument Origin requires CPython 3.12 through 3.15")
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        raise RuntimeError("Argument Origin requires a GIL-enabled interpreter")
    if _recording is not None or not 1 <= capacity <= 65536:
        raise ValueError("Recorder already active or invalid event capacity")
    recorder = Recording(root, capacity, on_break or _suspend)
    recorder.install()
    _recording = recorder
    return recorder


def stop():
    global _recording
    if _recording is not None:
        _recording.close()
        _recording = None


def export(frame_id, name, mode, path, watch_file=None):
    report = (_recording.query(frame_id, name, mode, watch_file) if _recording else
              dict(title=name, note="Unknown. Rerun with python/record.py to record before entry.", rows=[]))
    _write(path, report)
    return True


def _write(path, report):
    with open(path, "r+", encoding="utf-8") as out:
        json.dump(report, out)
        out.truncate()


def _watch_report(path, report):
    # Other debugger threads can pause while this thread publishes its hit.
    tmp = None
    try:
        if not os.path.isfile(path):
            return
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as out:
            tmp = out.name
            json.dump(report, out)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
