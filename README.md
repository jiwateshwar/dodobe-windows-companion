# dodobe companion (Windows)

A small tray app for the Windows PCs around a Dodobe box. It does three things:

1. **Stays in the notification area**: it starts at every sign-in, and one copy runs at a time.
2. **Opens Dodobe**: click the icon, or choose **Open Dodobe** (default `http://dodobe`, the same
   address the box's notifications link to).
3. **Keeps Dodobe's network folders on drive letters**, for example `L:` Library, `M:` Dumpsite,
   `S:` By species and `P:` Pool, across restarts.

## How the drives survive a restart

- The drives are mapped as Windows *remembered* (persistent) drives, the same as
  `net use L: \\dodobe\Library /persistent:yes`. Windows restores them at sign-in.
- The login is saved in **Credential Manager** under the box's name (`dodobe`), as `cmdkey` would.
  Windows uses it for that reconnect. It is never stored in a file.
- If the box wasn't reachable at sign-in (it was off, or the network came up late), Windows leaves the
  drive "unavailable". The companion checks every minute (every 20 s while something is wrong) and
  reconnects. It never takes over a letter used by a disk or by another network folder.
- If the box refuses the login (for example, the Dodobe password changed), it stops retrying. The dot
  turns red and a notification asks you to re-enter the password in Settings.

The login is the person's **Dodobe name and password**. On the box, Samba maps the name to its
`dbs-<name>` account (`agent/dodobe_agent.py`, `samba_apply`). The person's network login must be on:
in Dodobe, go to *Settings > Network folders* (`/account/network`).

## Using it

On first run the Settings window opens:

- **Address**: `dodobe` (or `dodobe.local`, or the box's IP address). **Homepage** is blank for
  `http://<address>`.
- **Dodobe name** and **password**.
- **Find folders** lists the box's visible network folders. Tick the ones you want and choose their
  letters. Use **A hidden folder by name** for a share set to "not visible".
- **Save** maps them.

The tray menu has: **Open Dodobe**, **Network drives** (each drive and its state; click one to open
it, or choose **Reconnect now**), **Settings…**, **Start with Windows**, **Open log** and **Quit**.

| Dot | Means |
|---|---|
| green | every chosen drive is connected |
| yellow | some are connected; the tooltip says what's wrong with the rest |
| red | the box can't be reached, or it refused the login |
| grey | not set up yet |

## Files

- Program: `%LOCALAPPDATA%\Programs\dodobe companion` (a per-user install, so no admin prompt).
- Settings and log: `%LOCALAPPDATA%\dodobe-companion\config.json` and `companion.log`.
- Login: Credential Manager > Windows Credentials > `dodobe`.

Uninstalling (*Settings > Apps*) stops the app, disconnects the drives it made, removes the saved
login and the start-up entry, and asks whether to delete the settings and log.

## Code

```
companion/
  dodobe_companion.pyw   the tray app (pystray) and the watcher thread; Tk runs on the main thread
  keeper.py              ensure (reconnect what's missing), apply (a settings change), find_shares, forget
  winnet.py              ctypes: Credential Manager, WNetAddConnection2/Cancel/GetConnection, NetShareEnum
  settings_window.py     the tkinter settings window
  config.py              settings, validation, drive-letter suggestions (no Windows calls; tested anywhere)
  dodobe-companion.spec  PyInstaller          dodobe-companion.iss  Inno Setup
tests/test_config.py        python3 tests/test_config.py
tests/windows_smb_test.py   Windows only (CI): maps a real SMB share on the runner
```

## Building

The installer is built and tested on Windows by GitHub Actions (`.github/workflows/companion-windows.yml`,
"Companion installer (Windows)") on every push. The run maps a real SMB share on the runner and
installs, starts, updates and uninstalls the app silently. **To release a version:** push a tag such
as `v1.0.1`. The run publishes `dodobe-companion-setup-<version>.exe` on the repo's Releases page, and
that file is the one to give people. On a Windows PC with Python 3.12+ and Inno Setup 6, you can also run
`companion\build.ps1`. To try it without building:
`py -3 -m pip install -r companion\requirements.txt` then `pyw -3 companion\dodobe_companion.pyw`.

It uses the same toolchain as the develop node's tray app (`Debian/dodobe_nas/deploy/develop/windows`)
and the same icon.
