"""A real test of the drive mapping, on Windows (the CI runner): against an SMB share on this PC,
made by the workflow (`\\\\localhost\\<share>`, readable and writable by a local user), standing in
for the Dodobe box. Usage: python tests/windows_smb_test.py <share> <user> <password>
"""

import sys
import time
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))

import keeper  # noqa: E402
import winnet  # noqa: E402
from config import Config, Drive  # noqa: E402

share, user, password = sys.argv[1:4]
HOST, LETTER = "localhost", "Q"


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        sys.exit(1)


def remembered(letter):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Network\{letter}") as k:
            return winreg.QueryValueEx(k, "RemotePath")[0]
    except OSError:
        return None


check(winnet.reachable(HOST), "the SMB port answers")
found = keeper.find_shares(HOST, user, password)
print("shares:", found)
check(any(n.lower() == share.lower() for n, _ in found), f"find_shares lists {share}")

cfg = Config(host=HOST, username=user, drives=[Drive(share, LETTER)])
states = keeper.apply(Config(), cfg, password)
print("states:", states)
check(all(s.state == keeper.OK for s in states), "apply connects the drive")
check(winnet.credential_user(HOST) == user, "the login is in Credential Manager")
kind, remote = winnet.mapping(f"{LETTER}:")
check(kind == "connected" and remote.lower() == cfg.unc(share).lower(), f"{LETTER}: is mapped to {remote}")
check((remembered(LETTER) or "").lower() == cfg.unc(share).lower(), "it is remembered for the next sign-in (HKCU\\Network)")
test_file = Path(f"{LETTER}:\\companion-test.txt")
test_file.write_text("hello")
check(test_file.read_text() == "hello", "a file can be written and read through the drive")
test_file.unlink()

check(all(s.state == keeper.OK for s in keeper.ensure(cfg)), "ensure leaves a connected drive alone")

winnet.disconnect(f"{LETTER}:", forget=False)  # what a restart with the box off leaves: remembered, not connected
time.sleep(1)
print("after a drop:", winnet.mapping(f"{LETTER}:"))
states = keeper.ensure(cfg)
check(all(s.state == keeper.OK for s in states), "ensure reconnects a dropped drive with the saved login")
check(winnet.mapping(f"{LETTER}:")[0] == "connected", f"{LETTER}: is connected again")

busy = Config(host=HOST, username=user, drives=[Drive(share, "C")])
check(keeper.ensure(busy)[0].state == keeper.PROBLEM, "a letter used by a disk is left alone")

# ---- uploads over the real share, with the share pulled away in the middle --------------------
import hashlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402

import logging  # noqa: E402

import uploader  # noqa: E402
import wol  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="  log %(name)s: %(message)s", force=True)

dest = cfg.unc(share)
src_root = Path(tempfile.mkdtemp()) / "Trip 2026-11"
(src_root / "Day 1").mkdir(parents=True)
big = src_root / "Day 1" / "BIG_0001.NEF"
with open(big, "wb") as f:
    for _ in range(64):
        f.write(os.urandom(8 * 1024 * 1024))  # 512 MB: long enough to pull the share away mid-file
(src_root / "Day 1" / "DSC_0002.JPG").write_bytes(os.urandom(300_000))
(src_root / "Thumbs.db").write_bytes(b"junk")

chunks = list(winnet.read_unbuffered(str(big)))
check(hashlib.sha256(b"".join(chunks)).hexdigest() == hashlib.sha256(big.read_bytes()).hexdigest(),
      "read_unbuffered reads a whole file")
del chunks
odd = Path(dest) / "odd sizes"
odd.mkdir(exist_ok=True)
for n in (0, 1, 511, 4095, 4096, 4097, 300_000, 4 * 1024 * 1024, 4 * 1024 * 1024 + 1, 9 * 1024 * 1024 + 7):
    data = os.urandom(n)
    (odd / f"{n}.bin").write_bytes(data)
    check(b"".join(winnet.read_unbuffered(str(odd / f"{n}.bin"))) == data, f"read_unbuffered over SMB: {n} bytes")
print("last pieces read the usual way:", winnet.tail_fallbacks, flush=True)
check(winnet.tail_fallbacks == 0, "every size is read back unbuffered over SMB")
for f in odd.iterdir():
    f.unlink()
odd.rmdir()

states = []
up = uploader.Uploader(Path(tempfile.mkdtemp()), online=lambda: winnet.reachable(HOST),
                       reader=winnet.read_unbuffered, machine="CI", wait_step=0.2)
job = up.add(src_root, dest)
up.on_change = lambda: states.append((job.state, job.message))
worker = threading.Thread(target=up.run, args=(job,))
worker.start()
deadline = time.monotonic() + 120
while up.progress.get(job.id, 0) < job.total * 0.2 and worker.is_alive() and time.monotonic() < deadline:
    time.sleep(0.05)
check(worker.is_alive(), f"the upload is under way ({job.state}: {job.message})")
print("pulling the share away at", up.progress.get(job.id), "of", job.total, flush=True)
ps = ["powershell", "-NoProfile", "-Command"]
import ctypes  # noqa: E402

code = ctypes.WinDLL("netapi32").NetShareDel(None, "Library", 0)  # at once (PowerShell takes seconds to start)
check(code == 0, f"the share is pulled away mid-file (NetShareDel {code})")
time.sleep(8)
subprocess.run(ps + [f"New-SmbShare -Name Library -Path C:\\dodobe-share -FullAccess {user} | Out-Null"], check=True,
               timeout=60)
print("the share is back", flush=True)
worker.join(300)
if worker.is_alive():
    up.cancel(job.id)  # don't leave the test hanging: say what it was doing, then stop
    worker.join(60)
print("upload:", job.state, job.message, flush=True)
check(job.state == "done", "the upload finished after the share came back")
waits = [m for st, m in states if st == "waiting"]
print("waits:", waits[:3], flush=True)
check(any("BIG_0001" in m for m in waits), "it waited in the middle of the big file while the share was gone")
check(not up.plain_check, "every checksum was read back unbuffered")
print("bytes sent:", up.sent.get(job.id), "for", job.total, flush=True)
check(up.sent.get(job.id, 0) < job.total * 1.1, "it carried on from where the box's copy ended (not from the start)")
final = Path(job.final)
check(final.parent.name == uploader.DROP and final.name == src_root.name, f"it landed in {final}")
for rel in ("Day 1/BIG_0001.NEF", "Day 1/DSC_0002.JPG"):
    a, b = src_root / rel, final / rel
    check(hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest(), f"{rel} is intact")
check(not (final / "Thumbs.db").exists(), "junk stays behind")
check(not (Path(dest) / uploader.STAGING).exists(), "the staging folder is cleaned away")

winnet.save_studio_login(HOST, user, "pw-ü-1")
check(winnet.studio_login(HOST) == (user, "pw-ü-1"), "the studio sign-in is saved and read back")
check(wol.send("00:1a:2b:3c:4d:5e", "127.0.0.1", repeat=1) > 0, "a wake-up packet can be sent")

keeper.forget(cfg)
check(winnet.studio_login(HOST) is None, "forget removes the studio sign-in too")
check(winnet.mapping(f"{LETTER}:")[0] == "free", "forget disconnects the drive")
check(remembered(LETTER) is None, "and Windows no longer remembers it")
check(winnet.credential_user(HOST) is None, "and the saved login is gone")
print("all SMB checks passed")
