"""Uploading a folder to Dodobe's dumpsite: checked, and carried on after any interruption.

Dodobe imports whatever lands in the dumpsite's "Drop here" once it has been still for a few
minutes (importing/drop.py on the box). A half-copied file must never be in there, so an upload
goes first to a staging folder beside it, `<dumpsite>\\.companion-uploads\\<PC>-<job>\\<folder>`,
and only when every file is there and checked does the whole folder move into "Drop here" in one
step (a rename on the box's disk: instant, nothing half-done for Dodobe to see).

- Checked: each file's SHA-256 is worked out from this PC's copy and from the box's copy, read back
  from the box's disk (not from Windows' cache). A file that doesn't match is sent again (3 tries).
- Carried on: when the connection drops, the upload waits for the box and continues from where the
  box's copy ends; the check at the end covers whatever was sent before. Jobs are saved in
  %LOCALAPPDATA%\\dodobe-companion\\uploads, so they carry on after the app or the PC restarts too.
- A file changed on this PC while it was being sent is sent again from the start.

The box's own import then sorts duplicates, junk and photographers as it does for every drop.
"""

import errno
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("uploader")

DROP = "Drop here"
STAGING = ".companion-uploads"
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
COPY_CHUNK = 1024 * 1024
TRIES = 3            # sends of one file that arrive damaged before the upload gives up
SAME_FAILURE = 3     # the same step failing this often in a row: send the file afresh (or check it plainly)
# Errors that only mean the box or its share can't be reached (the network is down, the share is
# gone for now, the connection was cut): waited out, never counted as a step that keeps failing.
GONE = {53, 59, 64, 67, 121, 240, 1203, 1222, 1231, 1232, 1236,
        errno.EBADF, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH}
WAIT_STEP = 1.0      # seconds between looks at the network (and at Pause/Cancel) while waiting
ACTIVE = ("queued", "copying", "waiting", "verifying", "moving")
FINISHED = ("done", "failed", "cancelled")


class Stopped(Exception):
    """Pause or Cancel."""


class Failed(Exception):
    """Something waiting won't fix."""


class DestError(Exception):
    """The box went away (or something like it): wait, then carry on."""

    def __init__(self, message: str, where: str = "", code=None):
        super().__init__(message)
        self.where, self.code = where, code


@dataclass
class Item:
    rel: str
    size: int
    mtime: float
    sha256: str = ""
    done: bool = False
    missing: bool = False


@dataclass
class Job:
    id: str
    source: str
    name: str
    dest_root: str                 # \\dodobe\Dumpsite
    machine: str
    created: float
    items: list[Item] = field(default_factory=list)
    photographer: str = ""         # optional: "Drop here\<photographer>\<folder>" makes it their batch
    state: str = "queued"
    message: str = ""
    final: str = ""
    finished: float = 0.0

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def staging(self) -> Path:
        return Path(self.dest_root) / STAGING / f"{self.machine}-{self.id}" / self.name


def junk(name: str) -> bool:
    return name.lower() in JUNK_NAMES or name.startswith("._")


def scan(source: Path) -> list[Item]:
    items = []
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not junk(d))
        for f in sorted(files):
            p = Path(root) / f
            if junk(f) or p.is_symlink():
                continue
            st = p.stat()
            items.append(Item(p.relative_to(source).as_posix(), st.st_size, st.st_mtime))
    return items


def _plain_reader(path: str, stop=None):
    with open(path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            if stop is not None and stop.is_set():
                return
            yield chunk


def _permanent(exc: OSError) -> str | None:
    """Errors that waiting won't fix, in words; None for the ones that come and go."""
    code = getattr(exc, "winerror", None)
    if code == 5 or (code is None and exc.errno in (errno.EACCES, errno.EPERM)):
        return "Your Dodobe login isn't allowed to save in the dumpsite."
    if code in (39, 112) or exc.errno == errno.ENOSPC:
        return "Dodobe's dumpsite is full."
    if code == 17 or exc.errno == errno.EXDEV:
        return "The dumpsite's staging folder is on another disk than Drop here."
    return None


class Uploader:
    def __init__(self, folder: Path, online, reader=None, on_change=None, machine: str | None = None,
                 wait_step: float = WAIT_STEP):
        self.folder = Path(folder)
        self.online = online                      # () -> bool: is the box reachable?
        self.reader = reader or _plain_reader     # how to read the box's copy back
        self.on_change = on_change or (lambda: None)
        self.machine = "".join(c for c in (machine or socket.gethostname()) if c.isalnum() or c in "-_")[:24] or "pc"
        self.wait_step = wait_step
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.jobs: dict[str, Job] = {}
        self.control: dict[str, str] = {}         # job id -> "pause" | "cancel"
        self.progress: dict[str, int] = {}        # job id -> bytes there (done files + the current one)
        self.rate: dict[str, float] = {}          # job id -> bytes per second
        self.plain_check: set[str] = set()        # jobs whose box can't be read back unbuffered
        self.sent: dict[str, int] = {}            # job id -> bytes written to the box (resends included)
        self.stopping = False
        self.thread: threading.Thread | None = None
        self._load()

    # ---- jobs ----------------------------------------------------------------------------------

    def _path(self, job: Job) -> Path:
        return self.folder / f"{job.id}.json"

    def _save(self, job: Job) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(job).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(job)), encoding="utf-8")
        os.replace(tmp, self._path(job))

    def _load(self) -> None:
        for p in sorted(self.folder.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                raw["items"] = [Item(**i) for i in raw.get("items", [])]
                job = Job(**raw)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("skipping %s: %s", p.name, exc)
                continue
            if job.state in ACTIVE:
                job.state, job.message = "queued", "Carrying on where it stopped"
            self.jobs[job.id] = job
            self.progress[job.id] = sum(i.size for i in job.items if i.done)
        for old in sorted((j for j in self.jobs.values() if j.state in FINISHED), key=lambda j: j.finished)[:-20]:
            self.forget(old.id)

    def add(self, source: str | Path, dest_root: str, photographer: str = "") -> Job:
        source = Path(source)
        if not source.is_dir():
            raise Failed(f"{source} isn't a folder.")
        items = scan(source)
        if not items:
            raise Failed(f"{source.name} has no files to upload.")
        job = Job(id=uuid.uuid4().hex[:10], source=str(source), name=source.name or "Upload", dest_root=dest_root,
                  machine=self.machine, created=time.time(), items=items, photographer=photographer.strip())
        with self.lock:
            self.jobs[job.id] = job
            self.progress[job.id] = 0
            self._save(job)
        log.info("queued %s: %d files, %d bytes -> %s", job.source, len(items), job.total, dest_root)
        self.wake.set()
        self.on_change()
        return job

    def pause(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.state in ACTIVE:
                self.control[job_id] = "pause"
                if job.state == "queued":
                    self._set(job, "paused", "Paused")
        self.wake.set()

    def resume(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            self.control.pop(job_id, None)
            if job and job.state in ("paused", "failed"):
                self._set(job, "queued", "Carrying on")
        self.wake.set()

    def cancel(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.state in FINISHED:
                return
            self.control[job_id] = "cancel"
            if job.state in ("queued", "paused"):  # not running: clean up on the worker
                job.state = "queued"
        self.wake.set()

    def forget(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.state in FINISHED:
                del self.jobs[job_id]
                self._path(job).unlink(missing_ok=True)
        self.on_change()

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [{"id": j.id, "name": j.name, "source": j.source, "state": j.state, "message": j.message,
                     "files": len(j.items), "files_done": sum(i.done for i in j.items), "total": j.total,
                     "done": self.progress.get(j.id, 0) if j.state != "done" else j.total,
                     "rate": self.rate.get(j.id, 0.0), "final": j.final}
                    for j in sorted(self.jobs.values(), key=lambda j: j.created)]

    def busy(self) -> bool:
        with self.lock:
            return any(j.state in ACTIVE for j in self.jobs.values())

    def _set(self, job: Job, state: str, message: str = "") -> None:
        job.state, job.message = state, message
        if state in FINISHED:
            job.finished = time.time()
        self._save(job)
        self.on_change()

    # ---- the worker ----------------------------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="uploads", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()

    def _next(self) -> Job | None:
        with self.lock:
            for job in sorted(self.jobs.values(), key=lambda j: j.created):
                if job.state in ACTIVE:
                    return job
        return None

    def _loop(self) -> None:
        while not self.stopping:
            job = self._next()
            if job is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.run(job)

    def run(self, job: Job) -> None:
        """Send one job to the end (or until it's paused, cancelled, or fails)."""
        try:
            self._check()(job)
            if not Path(job.source).is_dir() and not all(i.done or i.missing for i in job.items):
                raise Failed(f"{job.source} isn't on this PC any more. Plug it back in, then Resume.")
            for item in job.items:
                if item.done or item.missing:
                    continue
                opts = {"fresh": False, "plain": job.id in self.plain_check}
                fails: list = []
                while True:
                    try:
                        self._file(job, item, opts)
                        break
                    except DestError as exc:
                        if exc.code not in GONE:  # the box answers, yet this step keeps failing
                            fails.append((exc.where, exc.code))
                        if len(fails) >= SAME_FAILURE and len(set(fails[-SAME_FAILURE:])) == 1:
                            if exc.where == "checking" and not opts["plain"]:
                                opts["plain"] = True  # the unbuffered read is what fails: read it the usual way
                                self.plain_check.add(job.id)  # for the rest of this upload too
                                log.warning("%s: %s: reading back unbuffered keeps failing (%s); checking it "
                                            "with a normal read", job.id, item.rel, exc)
                            else:
                                opts["fresh"] = True
                                log.warning("%s: %s: the same step keeps failing (%s); sending it afresh",
                                            job.id, item.rel, exc)
                            fails.clear()
                        self._wait(job, str(exc))
            while True:
                try:
                    self._move(job)
                    break
                except DestError as exc:
                    self._wait(job, str(exc))
            missing = [i.rel for i in job.items if i.missing]
            note = f"; {len(missing)} file{'s' if len(missing) != 1 else ''} had gone from this PC" if missing else ""
            self._set(job, "done", f"In Drop here as {Path(job.final).name}: {len(job.items) - len(missing)} files, "
                                   f"all checked{note}")
            log.info("done %s -> %s%s", job.source, job.final, note)
        except Stopped:
            with self.lock:
                what = self.control.pop(job.id, None)
            if what == "cancel":
                self._clean(job)
                self._set(job, "cancelled", "Cancelled: nothing was put in Drop here")
                log.info("cancelled %s", job.source)
            elif what == "pause":
                self._set(job, "paused", "Paused")
            else:  # the app is quitting: carry on at the next start
                self._set(job, "queued", "Carries on when the companion starts again")
        except Failed as exc:
            self._set(job, "failed", str(exc))
            log.warning("failed %s: %s", job.source, exc)
        except Exception as exc:  # a bug, not the network: stop this job, keep the app
            log.exception("upload %s", job.id)
            self._set(job, "failed", f"Stopped: {exc}")

    def _check(self):
        def check(job: Job) -> None:
            if self.control.get(job.id) or self.stopping:
                raise Stopped()
        return check

    def _wait(self, job: Job, why: str) -> None:
        """Wait for the box to answer again; Pause and Cancel still work meanwhile."""
        log.info("%s: waiting (%s)", job.id, why)
        self._set(job, "waiting", f"Waiting for Dodobe: {why}")
        while True:
            for _ in range(10):
                self._check()(job)
                time.sleep(self.wait_step)
            self._check()(job)
            if self.online():
                self._set(job, "copying", "Carrying on")
                return

    def _dest(self, fn, *args):
        """A step on the box: OSError becomes DestError (wait and carry on) or Failed (it won't help)."""
        try:
            return fn(*args)
        except OSError as exc:
            raise self._error(exc) from exc

    def _error(self, exc: OSError) -> Exception:
        reason = _permanent(exc)
        if reason:
            return Failed(reason)
        code = getattr(exc, "winerror", None) or exc.errno
        where, rel = self.where
        log.info("%s %s: %r", where, rel, exc)
        return DestError(f"{exc.strerror or exc} ({where} {rel}, error {code})", where, code)

    where = ("preparing", "")

    def _at(self, step: str, rel: str) -> None:
        self.where = (step, rel)

    def _file(self, job: Job, item: Item, opts: dict | None = None) -> None:
        opts = opts if opts is not None else {"fresh": False, "plain": False}
        check = self._check()
        self._at("preparing", item.rel)
        src, dst = Path(job.source) / item.rel, job.staging / item.rel
        done_before = sum(i.size for i in job.items if i.done)
        for attempt in range(1, TRIES + 1):
            check(job)
            try:
                st = src.stat()
            except FileNotFoundError:
                item.missing = True
                self._save(job)
                log.warning("%s: %s is no longer on this PC", job.id, item.rel)
                return
            except OSError as exc:
                raise DestError(f"can't read {item.rel} on this PC ({exc.strerror})") from exc
            if st.st_size != item.size or st.st_mtime != item.mtime:  # changed here: start it again
                item.size, item.mtime, item.sha256 = st.st_size, st.st_mtime, ""
                self._dest(lambda: dst.unlink(missing_ok=True))
                self._save(job)
            self._dest(lambda: dst.parent.mkdir(parents=True, exist_ok=True))
            if opts["fresh"]:
                self._dest(lambda: dst.unlink(missing_ok=True))
                opts["fresh"] = False
            have = self._dest(lambda: dst.stat().st_size if dst.exists() else 0)
            if have > item.size:
                self._dest(dst.unlink)
                have = 0
            if job.state != "copying":
                self._set(job, "copying", f"Sending {item.rel}")
            else:
                job.message = f"Sending {item.rel}"
                self.on_change()
            self._send(job, src, dst, have, done_before, item.size)
            if not item.sha256:
                item.sha256 = self._sha(job, lambda: _plain_reader(str(src)), local=True)
                self._save(job)
            job.message = f"Checking {item.rel}"
            self.on_change()
            self._at("checking", item.rel)
            there = self._dest(lambda: dst.stat().st_size)
            reader = _plain_reader if opts["plain"] else self.reader
            theirs = self._sha(job, lambda: reader(str(dst)), local=False) if there == item.size else ""
            self._at("preparing", item.rel)
            if theirs == item.sha256:
                item.done = True
                self._save(job)
                return
            log.warning("%s: %s arrived damaged (try %d of %d); sending it again", job.id, item.rel, attempt, TRIES)
            self._dest(dst.unlink)
        raise Failed(f"{item.rel} kept arriving damaged ({TRIES} tries). Check the network, then Resume.")

    def _send(self, job: Job, src: Path, dst: Path, offset: int, done_before: int, size: int) -> None:
        check = self._check()
        window_t, window_b = time.monotonic(), offset
        try:
            fin = open(src, "rb")
        except FileNotFoundError:
            return  # gone meanwhile: the next look marks it missing
        with fin:
            fin.seek(offset)
            self._at("opening", dst.name)
            fout = self._dest(open, dst, "r+b" if offset else "wb")
            try:
                self._dest(fout.seek, offset)
                self._at("writing", dst.name)
                pos = offset
                while pos < size:
                    check(job)
                    try:
                        chunk = fin.read(min(COPY_CHUNK, size - pos))
                    except OSError as exc:  # a USB card pulled out, a sleeping disk
                        raise DestError(f"can't read {src.name} on this PC ({exc.strerror})") from exc
                    if not chunk:
                        break  # shorter than it was: the size check catches it next time
                    self._dest(fout.write, chunk)
                    pos += len(chunk)
                    self.sent[job.id] = self.sent.get(job.id, 0) + len(chunk)
                    self.progress[job.id] = done_before + pos
                    now = time.monotonic()
                    if now - window_t >= 1:
                        self.rate[job.id] = (pos - window_b) / (now - window_t)
                        window_t, window_b = now, pos
                        self.on_change()
                self._at("flushing", dst.name)
                self._dest(fout.flush)
                self._dest(os.fsync, fout.fileno())  # on the box's disk before it's checked
            finally:
                try:
                    fout.close()
                except OSError:
                    pass

    def _sha(self, job: Job, chunks, local: bool) -> str:
        stop = threading.Event()
        h = hashlib.sha256()
        try:
            for chunk in chunks():
                if self.control.get(job.id) or self.stopping:
                    stop.set()
                    raise Stopped()
                h.update(chunk)
        except OSError as exc:
            if local:
                raise DestError(f"can't read a file on this PC ({exc.strerror})") from exc
            raise self._error(exc) from exc
        return h.hexdigest()

    def _move(self, job: Job) -> None:
        """Everything is there and checked: into Drop here, in one rename."""
        drop = Path(job.dest_root) / DROP
        if job.photographer:
            drop = drop / job.photographer
        if job.final and self._dest(lambda: Path(job.final).exists() and not job.staging.exists()):
            return  # moved already (the app stopped just after)
        self._at("moving", job.name)
        job.state, job.message = "moving", "Putting it in Drop here"
        self._save(job)
        self.on_change()
        self._dest(lambda: drop.mkdir(parents=True, exist_ok=True))
        n, final = 1, drop / job.name
        while self._dest(final.exists):
            n += 1
            final = drop / f"{job.name} ({n})"
        job.final = str(final)
        self._save(job)
        self._dest(os.rename, job.staging, final)
        for empty in (job.staging.parent, job.staging.parent.parent):
            try:
                empty.rmdir()
            except OSError:
                pass  # not empty (another upload), or already gone

    def _clean(self, job: Job) -> None:
        import shutil
        try:
            shutil.rmtree(job.staging.parent, ignore_errors=True)
            (Path(job.dest_root) / STAGING).rmdir()
        except OSError:
            pass
