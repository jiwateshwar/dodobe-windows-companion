"""Uploads, waking the box and the studio's power actions: python3 tests/test_features.py (runs anywhere).
On the Windows runner, tests/windows_smb_test.py does an upload against a real SMB share as well."""

import errno
import hashlib
import http.server
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))

import studio  # noqa: E402
import uploader  # noqa: E402
import wol  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_source(root: Path) -> Path:
    src = root / "Kaziranga 2026-11"
    (src / "Day 1").mkdir(parents=True)
    (src / "Day 1" / "DSC_0001.NEF").write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    (src / "Day 1" / "DSC_0002.JPG").write_bytes(os.urandom(200_000))
    (src / "notes.txt").write_text("hello")
    (src / "empty.bin").write_bytes(b"")
    (src / "Thumbs.db").write_bytes(b"junk")
    (src / "._DSC_0001.NEF").write_bytes(b"junk")
    return src


def setup(tmp: Path, **kw):
    src = make_source(tmp)
    dump = tmp / "Dumpsite"
    (dump / uploader.DROP).mkdir(parents=True)
    up = uploader.Uploader(tmp / "state", online=lambda: True, machine="TESTPC", wait_step=0.001, **kw)
    return src, dump, up


def same_tree(src: Path, dst: Path) -> None:
    want = {p.relative_to(src).as_posix(): sha(p) for p in src.rglob("*")
            if p.is_file() and not uploader.junk(p.name)}
    got = {p.relative_to(dst).as_posix(): sha(p) for p in dst.rglob("*") if p.is_file()}
    assert got == want, (sorted(got), sorted(want))


def test_upload_lands_in_drop_here_whole():
    with tempfile.TemporaryDirectory() as d:
        src, dump, up = setup(Path(d))
        job = up.add(src, str(dump))
        assert len(job.items) == 4  # junk left out
        up.run(job)
        assert job.state == "done", job.message
        final = dump / uploader.DROP / src.name
        same_tree(src, final)
        assert not (dump / uploader.STAGING).exists()  # staging cleaned away
        assert all(i.sha256 for i in job.items)


def test_a_dropped_connection_is_waited_out_and_resumed():
    with tempfile.TemporaryDirectory() as d:
        src, dump, up = setup(Path(d))
        checks = {"n": 0}
        up.online = lambda: checks.__setitem__("n", checks["n"] + 1) or checks["n"] > 2  # back on the 3rd look
        real_open, sent = open, {"bytes": 0, "drops": 0}

        class Flaky:
            def __init__(self, f):
                self.f = f

            def write(self, b):
                if sent["drops"] == 0 and sent["bytes"] > 1024 * 1024:
                    sent["drops"] += 1
                    raise OSError(errno.ECONNRESET, "The network name is no longer available")
                sent["bytes"] += len(b)
                return self.f.write(b)

            def __getattr__(self, k):
                return getattr(self.f, k)

        def flaky_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            return Flaky(f) if "Dumpsite" in str(path) and ("w" in mode or "+" in mode) else f
        uploader.open = flaky_open
        try:
            job = up.add(src, str(dump))
            states = []
            up.on_change = lambda: states.append(job.state)
            up.run(job)
        finally:
            del uploader.open
        assert job.state == "done", job.message
        assert sent["drops"] == 1 and "waiting" in states
        assert checks["n"] >= 3
        assert sent["bytes"] < job.total * 1.2  # carried on; not sent again from the start
        assert up.sent[job.id] == sent["bytes"]
        same_tree(src, dump / uploader.DROP / src.name)


def test_a_damaged_copy_is_sent_again_and_never_lands():
    with tempfile.TemporaryDirectory() as d:
        tries = {"n": 0}

        def bad_once(path, stop=None):
            data = Path(path).read_bytes()
            if path.endswith("DSC_0002.JPG") and tries["n"] == 0:
                tries["n"] += 1
                data = data[:-1] + bytes([data[-1] ^ 1])
            yield data
        src, dump, up = setup(Path(d), reader=bad_once)
        job = up.add(src, str(dump))
        up.run(job)
        assert job.state == "done" and tries["n"] == 1
        same_tree(src, dump / uploader.DROP / src.name)

    with tempfile.TemporaryDirectory() as d:
        def always_bad(path, stop=None):
            yield b"not it"
        src, dump, up = setup(Path(d), reader=always_bad)
        job = up.add(src, str(dump))
        up.run(job)
        assert job.state == "failed" and "damaged" in job.message
        assert not any((dump / uploader.DROP).iterdir())  # nothing half-checked for Dodobe to import


def test_it_carries_on_after_the_app_restarts():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, dump, up = setup(tmp)
        job = up.add(src, str(dump))
        real = up._file
        calls = []

        def quit_after_one(job_, item, *opts):
            if calls:
                up.stopping = True  # the app quits while the second file is going
            calls.append(item.rel)
            real(job_, item, *opts)
        up._file = quit_after_one
        up.run(job)
        assert job.state == "queued" and sum(i.done for i in job.items) == 1
        first = next(i for i in job.items if i.done)
        staged = job.staging / first.rel
        mtime = staged.stat().st_mtime_ns

        again = uploader.Uploader(tmp / "state", online=lambda: True, machine="TESTPC", wait_step=0.001)
        job2 = again.jobs[job.id]
        assert job2.state == "queued" and sum(i.done for i in job2.items) == 1
        again.run(job2)
        assert job2.state == "done", job2.message
        same_tree(src, dump / uploader.DROP / src.name)
        assert (dump / uploader.DROP / src.name / first.rel).stat().st_mtime_ns == mtime  # not sent twice


def test_the_same_failure_over_and_over_changes_approach():
    with tempfile.TemporaryDirectory() as d:
        calls = {"n": 0}

        def unbuffered_broken(path, stop=None):
            calls["n"] += 1
            raise OSError(errno.EINVAL, "The parameter is incorrect")
            yield b""
        src, dump, up = setup(Path(d), reader=unbuffered_broken)
        job = up.add(src, str(dump))
        messages = []
        up.on_change = lambda: messages.append(job.message)
        up.run(job)
        assert job.state == "done", job.message
        assert calls["n"] == 3  # three times for the first file, then the normal read for every file
        assert any("checking" in m and "error" in m for m in messages)  # the wait names the step
        same_tree(src, dump / uploader.DROP / src.name)


def test_names_photographers_pause_and_cancel():
    with tempfile.TemporaryDirectory() as d:
        src, dump, up = setup(Path(d))
        (dump / uploader.DROP / src.name).mkdir()  # an earlier upload Dodobe hasn't taken yet
        job = up.add(src, str(dump), photographer="")
        up.run(job)
        assert Path(job.final).name == f"{src.name} (2)"

        job = up.add(src, str(dump), photographer="ana")
        up.run(job)
        assert Path(job.final).parent.name == "ana"

        job = up.add(src, str(dump))
        up.pause(job.id)
        assert job.state == "paused"
        up.resume(job.id)
        assert job.state == "queued"
        up.cancel(job.id)
        up.run(job)
        assert job.state == "cancelled"
        assert not (dump / uploader.STAGING).exists()
        assert len(list((dump / uploader.DROP).iterdir())) == 3  # 2 uploads + ana's folder; nothing from it

        try:
            up.add(Path(d) / "nope", str(dump))
            raise AssertionError("a missing folder was accepted")
        except uploader.Failed:
            pass


def test_magic_packet():
    p = wol.packet("00-1A-2b:3C4D5e")
    assert p[:6] == b"\xff" * 6 and p[6:] == bytes.fromhex("001a2b3c4d5e") * 16 and len(p) == 102
    assert wol.targets("192.168.0.246") == ["255.255.255.255", "192.168.0.255"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        s.settimeout(2)
        port = s.getsockname()[1]
        wol.PORTS, old = (port,), wol.PORTS
        try:
            assert wol.send("00:1a:2b:3c:4d:5e", None, repeat=1, extra=["127.0.0.1"]) >= 1
            data, _ = s.recvfrom(200)
        finally:
            wol.PORTS = old
        assert data == p


class FakeStudio(http.server.BaseHTTPRequestHandler):
    """The studio's login, power and logout, with its cross-site refusal (webutil.same_origin)."""
    calls: list = []
    role = "admin"

    def log_message(self, *a):
        pass

    def do_POST(self):
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"] or 0)).decode()).items()}
        if self.headers.get("Origin") != f"http://{self.headers['Host']}":
            return self._send(403, "Cross-site request refused.")
        FakeStudio.calls.append((self.path, form, self.headers.get("Cookie")))
        if self.path == "/login":
            if form.get("password") != "right":
                return self._send(401, "Wrong username or password.")
            return self._send(303, "", {"Location": "/settings/system", "Set-Cookie": "dodobe_session=abc; Path=/; HttpOnly; SameSite=strict"})
        if self.headers.get("Cookie") != "dodobe_session=abc":
            return self._send(303, "", {"Location": "/login"})
        if self.path == "/settings/system/power":
            if FakeStudio.role != "admin":
                return self._send(403, "<p>Your role in Dodobe can't open this page.</p>")
            if form.get("sure") != "1":
                return self._send(303, "", {"Location": "/settings/system?error=Tick+the+box"})
            return self._send(200, "<h1>Restarting</h1>")
        if self.path == "/logout":
            return self._send(303, "", {"Location": "/login"})
        self._send(404, "")

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        data = body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_studio_power():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeStudio)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        studio.power(base, "jiwateshwar", "right", "restart")
        paths = [c[0] for c in FakeStudio.calls]
        assert paths == ["/login", "/settings/system/power", "/logout"]
        assert FakeStudio.calls[1][1] == {"action": "restart", "sure": "1"}
        assert FakeStudio.calls[1][2] == "dodobe_session=abc"

        for password, role, words in (("wrong", "admin", "didn't accept"), ("right", "photographer", "Only a Dodobe Admin")):
            FakeStudio.role = role
            try:
                studio.power(base, "ana", password, "shutdown")
                raise AssertionError("no error")
            except studio.StudioError as exc:
                assert words in str(exc), exc
        try:
            studio.power("http://127.0.0.1:1", "a", "b", "restart")
            raise AssertionError("no error")
        except studio.StudioError as exc:
            assert "can't be reached" in str(exc)
    finally:
        server.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all ok")
