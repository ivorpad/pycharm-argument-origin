# Argument Origin for PyCharm

Argument Origin traces how a Python local reached a function, shows its assignment history, and pauses when the local changes.

## Install

1. In PyCharm, open **Settings → Plugins → ⚙ → Install Plugin from Disk** and select [argument-origin-0.4.1.zip](dist/argument-origin-0.4.1.zip). Restart if prompted.
2. Use a local GIL-enabled CPython 3.12–3.15 interpreter and select the **pydevd** debugger backend. The plugin has been tested with PyCharm 2026.3.0.1, build 263.7307.329.
3. Start an ordinary Python script debug session and pause at a breakpoint. Recording starts automatically for supported local script configurations.

## Use

Right-click a local in **Threads & Variables** and choose **Trace Argument Origin** to see its recorded path through supported calls and assignments. Expand a row for details, or select a file link to open its source. **Show Value History** lists retained assignments in execution order.

Choose **Pause on Value Changes** to arm a pause for a local in the current function invocation. Resume to stop after the local is assigned a different value. Assigning the same scalar value does not pause. Choose **Stop Pausing on Value Changes** to disarm it. The action is also available in the Watches pane; from an empty pane, enter the local name when prompted. Only one local per invocation can be armed at a time.

Try [example/pipeline.py](example/pipeline.py) with a breakpoint on line 2, or [example/watch_loop.py](example/watch_loop.py) for repeated value changes.

The recorder follows supported local bindings and some string transformations. It does not trace mutations inside objects, globals, closure cells or subprocesses. Opaque objects use identity to detect rebinding. Module and test-runner configurations, remote interpreters and debugpy are not supported by the IDE integration. Recording hot loops adds overhead. Set `ARG_ORIGIN_RECORD=0` in a run configuration to disable recording, or `ARG_ORIGIN_VALUES=redacted` to hide value previews.

## Build

The build needs JDK 25, Python 3.12 or later and a local PyCharm SDK. For example:

```sh
export JAVA_HOME="$(/usr/libexec/java_home -v 25)"
./gradlew check buildPlugin -PpycharmContents="/path/to/PyCharm.app/Contents" -Ppython=python3.12
```

The plugin ZIP is written to `build/distributions/`. The checks include recorder tests and a local pydevd smoke test.

## License

[MIT](LICENSE)
