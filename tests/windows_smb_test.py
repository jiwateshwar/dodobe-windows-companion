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

import uploader  # noqa: E402
import wol  # noqa: E402

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

states = []
up = uploader.Uploader(Path(tempfile.mkdtemp()), online=lambda: winnet.reachable(HOST),
                       reader=winnet.read_unbuffered, machine="CI", wait_step=0.2)
up.on_change = lambda: states.append(up.snapshot()[0]["state"]) if up.jobs else None
job = up.add(src_root, dest)
worker = threading.Thread(target=up.run, args=(job,))
worker.start()
while up.progress.get(job.id, 0) < job.total * 0.2 and worker.is_alive():
    time.sleep(0.05)
print("pulling the share away at", up.progress.get(job.id), "of", job.total, flush=True)
ps = ["powershell", "-NoProfile", "-Command"]
subprocess.run(ps + ["Remove-SmbShare -Name Library -Force"], check=True)
time.sleep(8)
subprocess.run(ps + [f"New-SmbShare -Name Library -Path C:\\dodobe-share -FullAccess {user} | Out-Null"], check=True)
worker.join(600)
print("upload:", job.state, job.message, flush=True)
check(job.state == "done", "the upload finished after the share came back")
check("waiting" in states, "it waited while the share was gone")
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
