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

keeper.forget(cfg)
check(winnet.mapping(f"{LETTER}:")[0] == "free", "forget disconnects the drive")
check(remembered(LETTER) is None, "and Windows no longer remembers it")
check(winnet.credential_user(HOST) is None, "and the saved login is gone")
print("all SMB checks passed")
