# Argument Origin for PyCharm

Argument Origin traces how a Python local reached a function, shows its assignment history, and pauses when the local changes.

## Install

1. In PyCharm, open **Settings → Plugins → ⚙ → Install Plugin from Disk** and select [argument-origin-0.4.4.zip](dist/argument-origin-0.4.4.zip). Restart if prompted.
2. Use a local GIL-enabled CPython 3.12–3.15 interpreter and select the **pydevd** debugger backend. Tested PyCharm builds are 2026.2.2 (`PY-262.10315.174`) and 2026.3.0.1 (`PY-263.7307.329`).
3. Start an ordinary Python script debug session and pause at a breakpoint. Recording starts automatically for supported local script configurations.

## Use

Right-click a local in **Threads & Variables** and choose **Trace Argument Origin** to see its recorded path through supported calls and assignments. Expand a row for details, or select a file link to open its source. **Show Value History** lists retained assignments in execution order.

Choose **Pause on Value Changes** to arm a pause for a local in the current function invocation. Resume to stop after the local is assigned a different value. Assigning the same scalar value does not pause. Choose **Stop Pausing on Value Changes** to disarm it. The action is also available in the Watches pane; from an empty pane, enter the local name when prompted. Only one local per invocation can be armed at a time.

Try [example/pipeline.py](example/pipeline.py) with a breakpoint on line 2, or [example/watch_loop.py](example/watch_loop.py) for repeated value changes.

The recorder follows supported local bindings and some string transformations. It does not trace mutations inside objects, globals, closure cells or subprocesses. Opaque objects use identity to detect rebinding. Module and test-runner configurations, remote interpreters and debugpy are not supported by the IDE integration. Recording hot loops adds overhead. Set `ARG_ORIGIN_RECORD=0` in a run configuration to disable recording, or `ARG_ORIGIN_VALUES=redacted` to hide value previews.

## Debugger MCP

Install [Debugger MCP Extension 0.1.7](https://raw.githubusercontent.com/ivorpad/pycharm-debugger-mcp/fcfbbb179c685857f8e28f8fccb4740aff282da4/dist/pycharm-debugger-mcp-0.1.7.zip) through **Install Plugin from Disk** and enable PyCharm’s bundled MCP Server. Call `debugger_setup` with the open project path to see launch choices. Use `mode=pydevd` to start or configure a Python Debug Server for a container. See the [MCP quick start](https://github.com/ivorpad/pycharm-debugger-mcp/blob/fcfbbb179c685857f8e28f8fccb4740aff282da4/README.md#quick-start).

With the updated Debugger MCP Extension installed, `debugger_origin` exposes recorded data in a paused local Python frame. Use `action=watch_once` and `name=payload`, then resume with `debugger_control`. The monitor disarms at the first change. After the debugger pauses, `action=watch_status` reports whether it is armed and its last hit; `action=unwatch` cancels it. A different breakpoint may pause execution first, so check the status. The monitor belongs to one function invocation and ends when that frame exits. Arming another monitor in the same frame replaces the previous one.

Use `action=trace` for the origin or `action=history` for assignments. Responses include `total` and `nextOffset`; pass `offset` and `limit` to page through rows, and `detail=true` to include the full event information. Set `changesOnly=true` on history to omit equal assignments and the initial argument. Each history row includes `eventId`, its assignment location, and its recognized producer. Pass that `eventId` to `action=trace` to expand the producer chain for that specific retained assignment. Events can expire; the tool reports when a selected event is unavailable. Recording and redaction settings apply to these results too.

## Build

Gradle runs on JDK 21. Kotlin targets Java 25 bytecode for PyCharm’s bundled runtime. The example uses Homebrew’s JDK 21, CPython 3.12 and a local PyCharm SDK.

```sh
export JAVA_HOME="$(brew --prefix openjdk@21)/libexec/openjdk.jdk/Contents/Home"
./gradlew check buildPlugin -PpycharmContents="/path/to/PyCharm.app/Contents" -Ppython=python3.12
```

The plugin ZIP is written to `build/distributions/`. The checks include recorder tests and a local pydevd smoke test.

## License

[MIT](LICENSE)
