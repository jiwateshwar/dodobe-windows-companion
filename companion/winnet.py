"""The Windows calls the companion needs, through ctypes (no pywin32, and no password on a command line):

- the network login, kept in Credential Manager as a Windows credential for the box (what
  `cmdkey /add:dodobe /user:<name> /pass` makes), so Windows itself can reconnect at sign-in;
- drive mappings, made "remembered" (persistent, what `net use Z: \\\\dodobe\\Library /persistent:yes`
  makes), so they're back after a restart;
- the box's network folders (NetShareEnum), its SMB port, and a single-instance lock.
"""

import ctypes
import socket
import sys
from ctypes import wintypes

if sys.platform != "win32":  # importable elsewhere for reading; every call needs Windows
    raise ImportError("winnet works only on Windows")

mpr = ctypes.WinDLL("mpr", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DWORD, LPWSTR, LPCWSTR = wintypes.DWORD, wintypes.LPWSTR, wintypes.LPCWSTR

RESOURCETYPE_ANY, RESOURCETYPE_DISK = 0, 1
RESOURCE_CONNECTED, RESOURCE_REMEMBERED = 1, 3
CONNECT_UPDATE_PROFILE = 0x1      # remember it: restored at every sign-in
CRED_TYPE_DOMAIN_PASSWORD = 2     # the kind Windows' file sharing uses
CRED_PERSIST_ENTERPRISE = 3       # kept across restarts (and roams with the profile)
STYPE_DISKTREE, STYPE_SPECIAL = 0, 0x80000000

NO_ERROR = 0
ERROR_ACCESS_DENIED = 5
ERROR_BAD_NETPATH = 53
ERROR_BAD_NET_NAME = 67
ERROR_ALREADY_ASSIGNED = 85
ERROR_INVALID_PASSWORD = 86
ERROR_MORE_DATA = 234
ERROR_NO_MORE_ITEMS = 259
ERROR_ALREADY_EXISTS = 183
ERROR_CONNECTION_UNAVAIL = 1201
ERROR_DEVICE_ALREADY_REMEMBERED = 1202
ERROR_NO_NET_OR_BAD_PATH = 1203
ERROR_SESSION_CREDENTIAL_CONFLICT = 1219
ERROR_NO_NETWORK = 1222
ERROR_LOGON_FAILURE = 1326
ERROR_NOT_CONNECTED = 2250
ERROR_NOT_FOUND = 1168

BAD_LOGIN = {ERROR_INVALID_PASSWORD, ERROR_LOGON_FAILURE}
UNREACHABLE = {ERROR_BAD_NETPATH, ERROR_NO_NET_OR_BAD_PATH, ERROR_NO_NETWORK}


class NETRESOURCEW(ctypes.Structure):
    _fields_ = [("dwScope", DWORD), ("dwType", DWORD), ("dwDisplayType", DWORD), ("dwUsage", DWORD),
                ("lpLocalName", LPWSTR), ("lpRemoteName", LPWSTR), ("lpComment", LPWSTR), ("lpProvider", LPWSTR)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", DWORD), ("Type", DWORD), ("TargetName", LPWSTR), ("Comment", LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", DWORD), ("AttributeCount", DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", LPWSTR), ("UserName", LPWSTR)]


class SHARE_INFO_1(ctypes.Structure):
    _fields_ = [("shi1_netname", LPWSTR), ("shi1_type", DWORD), ("shi1_remark", LPWSTR)]


mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(NETRESOURCEW), LPCWSTR, LPCWSTR, DWORD]
mpr.WNetCancelConnection2W.argtypes = [LPCWSTR, DWORD, wintypes.BOOL]
mpr.WNetGetConnectionW.argtypes = [LPCWSTR, LPWSTR, ctypes.POINTER(DWORD)]
mpr.WNetOpenEnumW.argtypes = [DWORD, DWORD, DWORD, ctypes.POINTER(NETRESOURCEW), ctypes.POINTER(wintypes.HANDLE)]
mpr.WNetEnumResourceW.argtypes = [wintypes.HANDLE, ctypes.POINTER(DWORD), ctypes.c_void_p, ctypes.POINTER(DWORD)]
mpr.WNetCloseEnum.argtypes = [wintypes.HANDLE]
advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), DWORD]
advapi32.CredReadW.argtypes = [LPCWSTR, DWORD, DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
advapi32.CredDeleteW.argtypes = [LPCWSTR, DWORD, DWORD]
advapi32.CredFree.argtypes = [ctypes.c_void_p]
netapi32.NetShareEnum.argtypes = [LPWSTR, DWORD, ctypes.POINTER(ctypes.c_void_p), DWORD, ctypes.POINTER(DWORD),
                                  ctypes.POINTER(DWORD), ctypes.POINTER(DWORD)]
netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLogicalDrives.restype = DWORD


class NetError(Exception):
    def __init__(self, code: int, what: str = ""):
        self.code = code
        super().__init__(explain(code, what))


def explain(code: int, what: str = "") -> str:
    """An error code in words a person can act on."""
    if code in BAD_LOGIN:
        return ("Dodobe didn't accept your name and password. Check them in Settings, and that your network "
                "login is on (Dodobe: Settings > Network folders).")
    if code in UNREACHABLE:
        return "The Dodobe box can't be reached: is it on, and is this PC on the same network?"
    if code == ERROR_BAD_NET_NAME:
        return f"Dodobe has no network folder called {what}." if what else "Dodobe has no network folder by that name."
    if code == ERROR_ACCESS_DENIED:
        return f"Your Dodobe login isn't allowed to open {what}." if what else "Your Dodobe login isn't allowed in."
    if code == ERROR_ALREADY_ASSIGNED:
        return f"{what} is already used by another drive: choose another letter." if what else "That letter is taken."
    if code == ERROR_SESSION_CREDENTIAL_CONFLICT:
        return ("Windows is already connected to the Dodobe box with a different login. Save the settings "
                "again to sign it out and back in with yours.")
    return f"{ctypes.FormatError(code).strip()} (error {code})"


# ---- the network login (Credential Manager) ------------------------------------------------------

def save_credential(host: str, username: str, password: str) -> None:
    blob = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW(Type=CRED_TYPE_DOMAIN_PASSWORD, TargetName=host, UserName=username,
                       CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=CRED_PERSIST_ENTERPRISE, Comment="dodobe companion: network folders")
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise NetError(ctypes.get_last_error())


def credential_user(host: str) -> str | None:
    """The name saved for the box, or None. (Windows never hands the password back.)"""
    pcred = ctypes.POINTER(CREDENTIALW)()
    if not advapi32.CredReadW(host, CRED_TYPE_DOMAIN_PASSWORD, 0, ctypes.byref(pcred)):
        return None
    try:
        return pcred.contents.UserName
    finally:
        advapi32.CredFree(pcred)


def delete_credential(host: str) -> None:
    if not advapi32.CredDeleteW(host, CRED_TYPE_DOMAIN_PASSWORD, 0):
        code = ctypes.get_last_error()
        if code != ERROR_NOT_FOUND:
            raise NetError(code)


# ---- connections ---------------------------------------------------------------------------------

def connect(local: str | None, remote: str, username: str | None = None, password: str | None = None,
            remember: bool = True) -> None:
    """Map `local` (e.g. "L:", or None for a connection without a letter) to `remote`. With no name
    and password, Windows uses the credential saved for the box."""
    res = NETRESOURCEW(dwType=RESOURCETYPE_DISK if local else RESOURCETYPE_ANY, lpLocalName=local, lpRemoteName=remote)
    code = mpr.WNetAddConnection2W(ctypes.byref(res), password, username, CONNECT_UPDATE_PROFILE if remember else 0)
    if code != NO_ERROR:
        raise NetError(code, local or remote.rsplit("\\", 1)[-1])


def disconnect(name: str, forget: bool = True) -> None:
    """Disconnect a drive letter ("L:") or a remote name; `forget` also drops it from the remembered list."""
    code = mpr.WNetCancelConnection2W(name, CONNECT_UPDATE_PROFILE if forget else 0, True)
    if code not in (NO_ERROR, ERROR_NOT_CONNECTED):
        raise NetError(code, name)


def mapping(local: str) -> tuple[str, str | None]:
    """What a drive letter is: ("connected", unc), ("remembered", unc) when Windows remembers it but
    it isn't connected now, ("local", None) for a disk on this PC, or ("free", None)."""
    size = DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    code = mpr.WNetGetConnectionW(local, buf, ctypes.byref(size))
    if code == NO_ERROR:
        return "connected", buf.value
    if code == ERROR_CONNECTION_UNAVAIL:
        return "remembered", buf.value
    bit = ord(local[0].upper()) - ord("A")
    return ("local" if kernel32.GetLogicalDrives() & (1 << bit) else "free"), None


def used_letters() -> set[str]:
    mask = kernel32.GetLogicalDrives()
    used = {chr(ord("A") + i) for i in range(26) if mask & (1 << i)}
    used |= {r.lpLocalName[0].upper() for r in _enum(RESOURCE_REMEMBERED) if r.lpLocalName}
    return used


def connections_to(host: str) -> list[str]:
    """Every current or remembered connection to the box: drive letters ("L:") and deviceless
    remote names (\\\\dodobe\\IPC$)."""
    prefix = "\\\\" + host.lower() + "\\"
    names = []
    for scope in (RESOURCE_CONNECTED, RESOURCE_REMEMBERED):
        for r in _enum(scope):
            if r.lpRemoteName and r.lpRemoteName.lower().startswith(prefix):
                name = r.lpLocalName or r.lpRemoteName
                if name not in names:
                    names.append(name)
    return names


class _Entry:
    def __init__(self, r: NETRESOURCEW):
        self.lpLocalName, self.lpRemoteName = r.lpLocalName, r.lpRemoteName


def _enum(scope: int) -> list[_Entry]:
    handle = wintypes.HANDLE()
    if mpr.WNetOpenEnumW(scope, RESOURCETYPE_ANY, 0, None, ctypes.byref(handle)) != NO_ERROR:
        return []
    out = []
    try:
        size = 16384
        buf = ctypes.create_string_buffer(size)
        while True:
            count, length = DWORD(0xFFFFFFFF), DWORD(size)
            code = mpr.WNetEnumResourceW(handle, ctypes.byref(count), buf, ctypes.byref(length))
            if code == ERROR_MORE_DATA:
                size = max(length.value, size * 2)
                buf = ctypes.create_string_buffer(size)
                continue
            if code != NO_ERROR:  # ERROR_NO_MORE_ITEMS or a failure: what we have is all we get
                break
            items = ctypes.cast(buf, ctypes.POINTER(NETRESOURCEW))
            out += [_Entry(items[i]) for i in range(count.value)]  # copy the strings out before buf is reused
    finally:
        mpr.WNetCloseEnum(handle)
    return out


def list_shares(host: str) -> list[tuple[str, str]]:
    """The box's visible network folders: (name, description). Needs a connection or a saved credential."""
    ptr, read, total, resume = ctypes.c_void_p(), DWORD(), DWORD(), DWORD()
    code = netapi32.NetShareEnum("\\\\" + host, 1, ctypes.byref(ptr), 0xFFFFFFFF, ctypes.byref(read),
                                 ctypes.byref(total), ctypes.byref(resume))
    if code not in (NO_ERROR, ERROR_MORE_DATA):
        raise NetError(code)
    try:
        items = ctypes.cast(ptr, ctypes.POINTER(SHARE_INFO_1))
        found = [(items[i].shi1_netname, items[i].shi1_remark or "") for i in range(read.value)
                 if items[i].shi1_type & 0xFF == STYPE_DISKTREE and not items[i].shi1_type & STYPE_SPECIAL]
    finally:
        netapi32.NetApiBufferFree(ptr)
    return sorted(found, key=lambda s: s[0].lower())


# ---- the rest ------------------------------------------------------------------------------------

def reachable(host: str, timeout: float = 3.0) -> bool:
    """Does the box answer on the file-sharing port?"""
    try:
        with socket.create_connection((host, 445), timeout=timeout):
            return True
    except OSError:
        return False


_mutex = None


def single_instance(name: str) -> bool:
    """True for the first copy of the app in this sign-in session; False if one is already running."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Local\\" + name)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
