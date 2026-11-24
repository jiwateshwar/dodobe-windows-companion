"""Keeps the chosen network folders on their drive letters.

Windows restores remembered drives at sign-in by itself, using the credential saved for the box;
when the box wasn't reachable then (the PC woke first, Wi-Fi came up late), the drive stays
"unavailable" with a red cross. `ensure` finds those and connects them again, and `apply` carries
out a change of settings.
"""

import logging
from dataclasses import dataclass

import winnet
from config import Config, Drive, removed

log = logging.getLogger("keeper")

OK, OFFLINE, LOGIN, PROBLEM = "ok", "offline", "login", "problem"


@dataclass
class DriveState:
    drive: Drive
    state: str
    message: str = ""


def ensure(cfg: Config, password: str | None = None) -> list[DriveState]:
    """Connect every chosen folder that isn't connected. Never touches a letter used by something else.
    Without a password, Windows signs in with the credential saved for the box."""
    if not cfg.configured:
        return []
    if not winnet.reachable(cfg.host):
        msg = f"{cfg.host} can't be reached"
        return [DriveState(d, OFFLINE, msg) for d in cfg.drives]
    states = []
    for d in cfg.drives:
        unc = cfg.unc(d.share)
        try:
            kind, remote = winnet.mapping(d.local)
            same = (remote or "").lower() == unc.lower()
            if kind == "connected" and same:
                states.append(DriveState(d, OK))
                continue
            if kind == "local" or (kind in ("connected", "remembered") and not same):
                what = f"{remote}" if remote else "a disk on this PC"
                states.append(DriveState(d, PROBLEM, f"{d.local} is already used by {what}"))
                continue
            if kind == "remembered":  # Windows' own reconnect at sign-in failed: forget it and map it afresh
                winnet.disconnect(d.local, forget=True)
            winnet.connect(d.local, unc, cfg.username if password else None, password)
            log.info("connected %s to %s", d.local, unc)
            states.append(DriveState(d, OK))
        except winnet.NetError as exc:
            log.warning("%s (%s): %s", d.local, unc, exc)
            state = LOGIN if exc.code in winnet.BAD_LOGIN else OFFLINE if exc.code in winnet.UNREACHABLE else PROBLEM
            states.append(DriveState(d, state, str(exc)))
    return states


def apply(old: Config, new: Config, password: str | None) -> list[DriveState]:
    """Carry out new settings: drop the drives no longer wanted, save the login if it changed
    (signing Windows out of the box first, as it allows one login per server), then connect."""
    for d in removed(old, new):
        kind, remote = winnet.mapping(d.local)
        if kind in ("connected", "remembered") and (remote or "").lower() == old.unc(d.share).lower():
            winnet.disconnect(d.local, forget=True)
            log.info("removed %s (%s)", d.local, remote)
    login_changed = (password is not None or new.username != old.username or new.host.lower() != old.host.lower()
                     or winnet.credential_user(new.host) != new.username)
    if login_changed and new.username:
        if password is None:
            raise winnet.NetError(winnet.ERROR_LOGON_FAILURE)
        sign_out(new.host)
        winnet.save_credential(new.host, new.username, password)
        log.info("saved the network login %s for %s", new.username, new.host)
    return ensure(new, password if login_changed else None)


def sign_out(host: str) -> None:
    """Disconnect everything this PC has open on the box (current and remembered)."""
    for name in winnet.connections_to(host):
        try:
            winnet.disconnect(name, forget=True)
        except winnet.NetError as exc:
            log.warning("couldn't disconnect %s: %s", name, exc)


def find_shares(host: str, username: str, password: str | None) -> list[tuple[str, str]]:
    """The box's network folders. Tries with what Windows already has, then signs in with the
    name and password typed in the settings window."""
    if not winnet.reachable(host):
        raise winnet.NetError(winnet.ERROR_BAD_NETPATH)
    try:
        return winnet.list_shares(host)
    except winnet.NetError as exc:
        if not (password and username) or exc.code in winnet.UNREACHABLE:
            raise
    ipc = rf"\\{host}\IPC$"
    try:
        winnet.connect(None, ipc, username, password, remember=False)
    except winnet.NetError as exc:
        if exc.code != winnet.ERROR_SESSION_CREDENTIAL_CONFLICT:
            raise
        sign_out(host)  # signed in to the box as someone else: start again with this login
        winnet.connect(None, ipc, username, password, remember=False)
    return winnet.list_shares(host)


def forget(cfg: Config) -> None:
    """Uninstalling: disconnect the drives the companion made and remove the saved login."""
    for d in cfg.drives:
        try:
            kind, remote = winnet.mapping(d.local)
            if kind in ("connected", "remembered") and (remote or "").lower() == cfg.unc(d.share).lower():
                winnet.disconnect(d.local, forget=True)
        except winnet.NetError as exc:
            log.warning("couldn't disconnect %s: %s", d.local, exc)
    try:
        winnet.delete_credential(cfg.host)
    except winnet.NetError as exc:
        log.warning("couldn't remove the saved login: %s", exc)
