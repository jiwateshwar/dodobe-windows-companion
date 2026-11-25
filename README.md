# dodobe companion (Windows)

A small tray app for the Windows PCs around a Dodobe box. It does these things:

1. **Stays in the notification area**: it starts at every sign-in, and one copy runs at a time.
2. **Opens Dodobe**: click the icon, or choose **Open Dodobe** (default `http://dodobe`, the same
   address the box's notifications link to).
3. **Keeps Dodobe's network folders on drive letters**, for example `L:` Library, `M:` Dumpsite,
   `S:` By species and `P:` Pool, across restarts.
4. **Uploads folders to the dumpsite**, with every file checked and the upload carried on after any
   drop (below).
5. **Restarts or shuts down the box** (Dodobe Admins only), and **wakes it up** with a wake-on-LAN
   packet.

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

## Uploading a folder

Choose **Upload a folder to Dodobe…** (or the button in **Uploads…**), pick a folder and confirm. It
goes to the dumpsite's **Drop here** folder (`\\dodobe\Dumpsite\Drop here`). Dodobe imports it from
there, as it does any drop.

- **Nothing half-copied reaches Drop here.** Dodobe imports whatever has sat still in Drop here for a
  few minutes, so the files go first to `Dumpsite\.companion-uploads\<PC>-<job>\`. When every file
  is there and checked, the whole folder moves into Drop here in one rename. If a folder of the same
  name is already waiting there, it becomes "<name> (2)".
- **Checksums.** Each file's SHA-256 is computed from this PC's copy and from the box's copy. The box's
  copy is read back unbuffered, so the check sees what's on the box's disk, not Windows' cache. A file
  that doesn't match is sent again, up to 3 times, before the upload stops with the reason.
- **Resume and retry.** If the connection drops, the upload waits (yellow "Waiting for Dodobe") and
  carries on from where the box's copy ends. The final checksum covers the bytes sent before the drop.
  Upload jobs are saved in `%LOCALAPPDATA%\dodobe-companion\uploads`, so they also carry on after the
  app or the PC restarts. A file changed on this PC mid-upload is sent again from the start. A file
  deleted from this PC is skipped and reported.
- **Uploads…** shows each upload's progress and speed, with **Pause**, **Resume** and **Cancel**. Cancel
  removes what was sent. A notification says when an upload finishes or stops.
- It skips the junk Dodobe would delete anyway (`Thumbs.db`, `desktop.ini`, `.DS_Store`, `._*`).
  Duplicates, photographers and file types are handled by Dodobe's import as usual.

## Restarting, shutting down and waking the box

From the **Dodobe box** menu:

- **Restart…** and **Shut down…** ask you to confirm, then sign in to the studio on the local network
  (`http://dodobe`). They send the same request as *Settings > System > Restart / Shut down*, and sign
  out. Only a **Dodobe Admin** can do this; the studio refuses anyone else. The password is the one
  saved in Settings, kept as a Windows credential (`dodobe-companion:studio:<box>`) because Windows
  never hands back the file-sharing one. The box stops Dodobe cleanly (the database closes first) and
  then powers off or reboots.
- **Wake it up** sends a wake-on-LAN magic packet to the box's network card. It goes to
  255.255.255.255 and to the box's network's broadcast address, on UDP ports 9 and 7, three times. It
  then watches for the box for 5 minutes and tells you when it's up. The companion learns the box's
  network card (MAC) address by itself whenever the box is on (saved in `box.json`), or you can type it
  in Settings.

For waking to work:

- **The box arms its network card.** The agent sets `802-3-ethernet.wake-on-lan magic` on the wired
  connection at every start (`wake_on_lan` in `agent/dodobe_agent.py`), and with `ethtool` too where
  it's installed.
- **The box's BIOS must allow it.** Look for "Wake on LAN", "Power on by PCI-E" or "Resume by LAN",
  and turn off "ErP"/"deep sleep" if there is such an option. A shut-down box then wakes like a
  sleeping one.
- **The box must be on a network cable, on the same network as the PC.** Wi-Fi can't be woken this way,
  and routers don't pass the broadcast between networks.

## Using it

On first run the Settings window opens:

- **Address**: `dodobe` (or `dodobe.local`, or the box's IP address). **Homepage** is blank for
  `http://<address>`.
- **Dodobe name** and **password**.
- **Find folders** lists the box's visible network folders. Tick the ones you want and choose their
  letters. Use **A hidden folder by name** for a share set to "not visible".
- **Save** maps them.

The tray menu has:

- **Open Dodobe**
- **Network drives**: each drive and its state. Click one to open it, or choose **Reconnect now**.
- **Upload a folder to Dodobe…** and **Uploads…**
- **Dodobe box**: **Wake it up**, **Restart…** and **Shut down…**
- **Settings…**, **Start with Windows**, **Open log** and **Quit**

Settings also has the **Dumpsite folder** (usually `Dumpsite`) and the box's **Network card**
(blank: the one learned).

| Dot | Means |
|---|---|
| green | every chosen drive is connected |
| yellow | some are connected; the tooltip says what's wrong with the rest |
| red | the box can't be reached, or it refused the login |
| grey | not set up yet |

## Files

- Program: `%LOCALAPPDATA%\Programs\dodobe companion` (a per-user install, so no admin prompt).
- Settings and log: `%LOCALAPPDATA%\dodobe-companion\config.json` and `companion.log`.
- Login: Credential Manager > Windows Credentials > `dodobe` (file sharing) and Generic Credentials >
  `dodobe-companion:studio:dodobe` (restart and shut down).
- Uploads: `%LOCALAPPDATA%\dodobe-companion\uploads\*.json` (one per upload; the last 20 finished
  ones are kept). The box's address and network card: `box.json`.

Uninstalling (*Settings > Apps*) stops the app, disconnects the drives it made, removes the saved
logins and the start-up entry, and asks whether to delete the settings and log.

## Code

```
companion/
  dodobe_companion.pyw   the tray app (pystray) and the watcher thread; Tk runs on the main thread
  keeper.py              ensure (reconnect what's missing), apply (a settings change), find_shares, forget
  winnet.py              ctypes: Credential Manager, WNetAddConnection2/Cancel/GetConnection, NetShareEnum
  settings_window.py     the tkinter settings window
  uploader.py            uploads: staging, checksums, waiting out drops, resuming, the move into Drop here
  uploads_window.py      the Uploads window
  studio.py              restart and shut down through the studio's own form (sign in, post, sign out)
  wol.py                 the wake-on-LAN magic packet
  config.py              settings, validation, drive-letter suggestions (no Windows calls; tested anywhere)
  dodobe-companion.spec  PyInstaller          dodobe-companion.iss  Inno Setup
tests/test_config.py        python3 tests/test_config.py
tests/test_features.py      python3 tests/test_features.py (uploads with drops and damage, wake-up, a fake studio)
tests/windows_smb_test.py   Windows only (CI): maps a real SMB share, and uploads to it with the share
                            pulled away mid-file
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
