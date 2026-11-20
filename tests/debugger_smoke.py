"""Drive the pydevd shipped in the target IDE over a loopback debugger session."""
import argparse
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.parse import unquote
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def smoke(contents, launcher, loop=False):
    pydevd = contents / "plugins/python-ce/helpers/pydev/pydevd.py"
    sample = ROOT / "example" / ("watch_loop.py" if loop else "pipeline.py")
    with tempfile.TemporaryDirectory(dir=ROOT / ".local") as tmp, socket.socket() as listener:
        exchange = Path(tmp) / "report.json"
        hit = Path(tmp) / "hit.json"
        for file in (hit, exchange):
            file.touch(mode=0o600)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        cmd = [sys.executable, str(pydevd), "--client", "127.0.0.1", "--port",
               str(listener.getsockname()[1]), "--file", str(launcher), str(sample)]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={**os.environ, "PYTHONUNBUFFERED": "1"}) as child:
            try:
                sock, _ = listener.accept()
                with sock, sock.makefile("rb") as replies:
                    sock.settimeout(15)
                    sequences = itertools.count(1, 2)

                    def send(kind, *fields):
                        seq = next(sequences)
                        data = "\t".join(map(str, (kind, seq, *fields))) + "\n"
                        sock.sendall(data.encode())
                        return seq

                    def receive(kind, seq=None):
                        while raw := replies.readline():
                            k, n, payload = raw.decode().rstrip("\r\n").split("\t", 2)
                            if int(k) == 901:
                                raise AssertionError(unquote(payload))
                            if int(k) == kind and (seq is None or int(n) == seq):
                                return unquote(payload)
                        raise AssertionError("Debugger disconnected")

                    def paused(line):
                        thread = ET.fromstring(receive(105)).find("thread")
                        frame = thread.find("frame")
                        assert Path(unquote(frame.attrib["file"])) == sample
                        assert int(frame.attrib["line"]) == line, (line, frame.attrib)
                        return thread.attrib["id"], frame.attrib["id"]

                    def query(ids, mode):
                        thread, frame = ids
                        expr = f"__import__('argument_origin').export({frame}, 'payload', {mode!r}, {str(exchange)!r}, {str(hit)!r})"
                        seq = send(113, thread, frame, "LOCAL", expr, 0)
                        response = receive(113, seq)
                        assert "True" in response, response
                        return json.loads(exchange.read_text())

                    send(501, "1.0", "UNIX", "ID")
                    send(111, 1, "python-line", sample, 2, "None", "", "", "", "False", "NONE")
                    send(101, "")
                    ids = paused(2)
                    if loop:
                        assert "Armed" in query(ids, "watch")["note"]
                        for before, after in [(-1, 10), (10, 7000), (7000, 9800)]:
                            send(106, ids[0])
                            assert paused(6) == ids
                            row = json.loads(hit.read_text())["rows"][0]
                            assert (row["previous"], row["value"]) == (str(before), str(after)), row
                            print(f"Loop change {before} → {after}, assignment line {row['location']['line']}")
                            if after == 10:
                                send(108, ids[0])  # CMD_STEP_OVER
                                assert paused(3) == ids
                                print("Step Over reached the loop header with the watch still armed")
                        assert "Stopped watching" in query(ids, "unwatch")["note"]
                        send(106, ids[0])
                        out, err = child.communicate(timeout=15)
                        assert child.returncode == 0 and b"finished 9899" in out, (out, err)
                        print("Disarmed before the fourth change and completed all 9,900 iterations")
                        return
                    origin = query(ids, "trace")
                    assert [r["location"]["line"] for r in origin["rows"]] == [18, 13, 8, 9, 1], origin
                    print("Native line breakpoint at process:2; origin lines 18 → 13 → 8 → 9 → 1")
                    assert "Armed" in query(ids, "watch")["note"]
                    send(106, ids[0])
                    again = paused(4)
                    assert again == ids, (ids, again)
                    history = query(again, "history")
                    assert len(history["rows"]) == 2 and "'parcel-17' → 'PARCEL-17'" in history["rows"][1]["detail"]
                    assert json.loads(hit.read_text())["rows"][0]["location"]["line"] == 3
                    print("Watch suspended the same frame at process:4 after store on line 3; both values verified")
                    send(106, again[0])
                    out, err = child.communicate(timeout=15)
                    assert child.returncode == 0 and b"sent PARCEL-17" in out, (out, err)
                    print("Resume finished with exit code 0")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--contents", type=Path, default=ROOT / ".local/PyCharm.app/Contents")
    parser.add_argument("--launcher", type=Path, default=ROOT / "python/record.py")
    args = parser.parse_args()
    (ROOT / ".local").mkdir(exist_ok=True)
    smoke(args.contents.resolve(), args.launcher.resolve())
    smoke(args.contents.resolve(), args.launcher.resolve(), loop=True)
