"""The companion's settings: which Dodobe box, its homepage, the network login's name, and which
network folders go on which drive letters. Plain logic, no Windows calls, so it is testable anywhere.

The password is never here: it lives in Windows' Credential Manager (see winnet.py).
"""

import json
import os
import re
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_HOST = "dodobe"
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")       # a name or an IPv4 address
SHARE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")                   # as the NAS allows (dodobe_agent.py)
LOGIN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,27}$")                          # a Dodobe username
MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$")
DEFAULT_DUMPSITE = "Dumpsite"
LETTERS = [c for c in string.ascii_uppercase if c >= "D"]                    # A-C are floppies and the system drive
PREFERRED = {"library": "L", "dumpsite": "M", "by species": "S", "pool": "P"}  # Dumpsite: M, as D is usually taken


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "dodobe-companion"


@dataclass
class Drive:
    share: str
    letter: str

    @property
    def local(self) -> str:
        return f"{self.letter}:"


@dataclass
class Config:
    host: str = DEFAULT_HOST
    home_url: str = ""          # empty: http://<host>
    username: str = ""
    drives: list[Drive] = field(default_factory=list)
    dumpsite: str = DEFAULT_DUMPSITE   # the network folder uploads go to (its "Drop here" folder)
    mac: str = ""                      # the box's network card, for waking it; empty: the one learned

    @property
    def homepage(self) -> str:
        return self.home_url.strip() or f"http://{self.host}"

    def unc(self, share: str) -> str:
        return rf"\\{self.host}\{share}"

    @property
    def studio(self) -> str:
        """The studio on the local network (the box's gateway), for restart and shut-down."""
        return f"http://{self.host}"

    @property
    def configured(self) -> bool:
        return bool(self.username and self.drives)


def validate(cfg: Config) -> list[str]:
    """What's wrong with these settings, in words for the settings window (empty: fine)."""
    problems = []
    if not HOST.match(cfg.host or ""):
        problems.append("The Dodobe address is a name such as dodobe, or an address such as 192.168.0.246.")
    if cfg.home_url and not re.match(r"^https?://\S+$", cfg.home_url.strip()):
        problems.append("The homepage starts with http:// or https://.")
    if cfg.username and not LOGIN.match(cfg.username):
        problems.append("Your Dodobe name is up to 28 lower-case letters, digits, dots, dashes or underscores.")
    if not SHARE.match(cfg.dumpsite or ""):
        problems.append("Uploads need the name of Dodobe's dumpsite network folder (usually Dumpsite).")
    if cfg.mac and not MAC.match(cfg.mac.strip()):
        problems.append("The network card address looks like 00:1A:2B:3C:4D:5E.")
    letters, shares = set(), set()
    for d in cfg.drives:
        if not SHARE.match(d.share):
            problems.append(f"{d.share!r} isn't a network folder name Dodobe uses.")
        if d.letter not in LETTERS:
            problems.append(f"{d.share}: choose a drive letter from D to Z.")
        if d.letter in letters:
            problems.append(f"Two folders are on {d.letter}:.")
        if d.share.lower() in shares:
            problems.append(f"{d.share} is mapped twice.")
        letters.add(d.letter)
        shares.add(d.share.lower())
    return problems


def suggest_letter(share: str, taken: set[str]) -> str | None:
    """A free letter for a share: its usual one if free, else the highest free letter (Z, Y, ...)."""
    preferred = PREFERRED.get(share.lower())
    if preferred and preferred not in taken:
        return preferred
    for letter in reversed(LETTERS):
        if letter not in taken:
            return letter
    return None


def removed(old: Config, new: Config) -> list[Drive]:
    """Drives the companion mapped before and must disconnect now: no longer wanted, moved to
    another letter, or pointing at another box."""
    keep = {(new.host.lower(), d.share.lower(), d.letter) for d in new.drives}
    return [d for d in old.drives if (old.host.lower(), d.share.lower(), d.letter) not in keep]


def load(path: Path | None = None) -> Config:
    path = path or data_dir() / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Config()
    drives = [Drive(str(d.get("share", "")), str(d.get("letter", "")).upper()[:1])
              for d in raw.get("drives") or [] if isinstance(d, dict)]
    return Config(host=str(raw.get("host") or DEFAULT_HOST), home_url=str(raw.get("home_url") or ""),
                  username=str(raw.get("username") or ""), drives=drives,
                  dumpsite=str(raw.get("dumpsite") or DEFAULT_DUMPSITE), mac=str(raw.get("mac") or ""))


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or data_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def normal_mac(mac: str) -> str:
    """00:1a:2b:3c:4d:5e, from any of the usual ways of writing it."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).lower() if len(digits) == 12 else ""


def load_box(path: Path | None = None) -> dict:
    """What the companion learned about the box while it was on: {"mac": ..., "ip": ...}."""
    path = path or data_dir() / "box.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_box(box: dict, path: Path | None = None) -> None:
    path = path or data_dir() / "box.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(box, indent=2), encoding="utf-8")
    os.replace(tmp, path)
