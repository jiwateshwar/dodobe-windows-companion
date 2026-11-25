"""dodobe companion: the Windows tray app.

Sits in the notification area from sign-in, opens the Dodobe homepage, and keeps Dodobe's network
folders on drive letters: Windows remembers them and restores them after a restart, and the
companion checks them every minute and reconnects any that dropped (the box was off, the PC woke
before the network).

It also uploads folders to Dodobe's dumpsite (checked file by file, carried on after any drop; see
uploader.py), restarts or shuts the box down for an Admin (studio.py), and wakes it with a
wake-on-LAN packet (wol.py).

Its icon's dot: green, every drive connected; yellow, some aren't; red, the box can't be reached
or didn't accept the login; grey, not set up yet.

Settings and log: %LOCALAPPDATA%\\dodobe-companion (config.json, companion.log). The password is in
Windows' Credential Manager, under the box's name.

    dodobe-companion.exe            run (one copy per sign-in)
    dodobe-companion.exe --forget   disconnect its drives and remove the saved login (the uninstaller)
"""

import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FROZEN = getattr(sys, "frozen", False)
RESOURCES = Path(getattr(sys, "_MEIPASS", HERE))

import config  # noqa: E402
import keeper  # noqa: E402
import studio  # noqa: E402
import uploader  # noqa: E402
import winnet  # noqa: E402
import wol  # noqa: E402

APP = "dodobeCompanion"
TITLE = "dodobe companion"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CHECK_OK, CHECK_TROUBLE = 60, 20  # seconds between checks: all well, something to fix
LEARN_EVERY = 600                 # seconds between looks at the box's network card (for waking it)
WAKE_WAIT = 300                   # seconds to watch for the box after sending a wake-up
DOTS = {"ok": (46, 204, 113), "some": (241, 196, 15), "bad": (231, 76, 60), "idle": (149, 165, 166)}
log = logging.getLogger("companion")


def start_with_windows(enabled: bool | None = None) -> bool:
    """Read (or set) the per-user Run entry that starts the companion at sign-in."""
    import winreg
    if FROZEN:
        command = f'"{sys.executable}"'
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).resolve()}"'
    if enabled is not None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, APP)
                except FileNotFoundError:
                    pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, APP)[0])
    except OSError:
        return False


def _opener(local: str):
    """A menu action that opens a drive in Explorer (pystray calls actions by their argument count,
    so no default arguments here)."""
    return lambda: os.startfile(local + "\\")


def alert(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x40)
    except Exception:
        log.warning(text)


class App:
    def __init__(self):
        import pystray
        from PIL import Image

        self.cfg = config.load()
        self.states: list[keeper.DriveState] = []
        self.lock = threading.Lock()       # one check or settings change at a time
        self.wake = threading.Event()      # "check now"
        self.stopping = False
        self.login_failed = False          # don't keep trying a password the box refused
        self.notified = ""
        self.tasks: queue.Queue = queue.Queue()  # work for the Tk thread, from the tray's
        self.settings = None
        self.uploads_window = None
        self.learned_at = 0.0
        self.job_states: dict[str, str] = {}
        self.menu_at = 0.0
        self.uploader = uploader.Uploader(config.data_dir() / "uploads", online=lambda: winnet.reachable(self.cfg.host),
                                          reader=winnet.read_unbuffered, on_change=self.uploads_changed)

        self.root = tk.Tk()
        self.root.withdraw()
        icon = RESOURCES / "icon.png"
        self.base = Image.open(icon).convert("RGBA") if icon.exists() else Image.new("RGBA", (64, 64), (28, 51, 44, 255))
        item, menu = pystray.MenuItem, pystray.Menu
        self.icon = pystray.Icon(APP, self.picture(), TITLE, menu(
            item("Open Dodobe", self.open_home, default=True),
            item("Network drives", menu(self.drive_items)),
            item("Upload a folder to Dodobe…", lambda: self.tasks.put(self.pick_upload)),
            item(lambda _i: self.uploads_label(), lambda: self.tasks.put(self.open_uploads)),
            item("Dodobe box", menu(
                item("Wake it up", self.wake_box),
                item("Restart…", lambda: self.tasks.put(lambda: self.power("restart"))),
                item("Shut down…", lambda: self.tasks.put(lambda: self.power("shutdown"))),
            )),
            menu.SEPARATOR,
            item("Settings…", lambda: self.tasks.put(self.open_settings)),
            menu.SEPARATOR,
            item("Start with Windows", self.toggle_autostart, checked=lambda _i: start_with_windows()),
            item("Open log", lambda: os.startfile(config.data_dir() / "companion.log")),
            item("Quit", self.quit),
        ))

    # ---- the tray ------------------------------------------------------------------------------

    def drive_items(self):
        import pystray
        item = pystray.MenuItem
        if not self.cfg.configured:
            return (item("Not set up yet: open Settings…", lambda: self.tasks.put(self.open_settings)),)
        by_letter = {s.drive.letter: s for s in self.states}
        items = []
        for d in self.cfg.drives:
            s = by_letter.get(d.letter)
            ok = bool(s and s.state == keeper.OK)
            label = f"{d.local}  {d.share}" + ("" if ok else f"  ({s.message[:50]})" if s else "  (checking…)")
            items.append(item(label, _opener(d.local), enabled=ok))
        items += [pystray.Menu.SEPARATOR, item("Reconnect now", self.reconnect)]
        return tuple(items)

    def overall(self) -> tuple[str, str]:
        kind, text = self._drives_overall()
        jobs = [j for j in self.uploader.snapshot() if j["state"] in uploader.ACTIVE]
        if jobs:
            done, total = sum(j["done"] for j in jobs), sum(j["total"] for j in jobs) or 1
            text += f"; uploading {100 * done // total}%"
        return kind, text

    def _drives_overall(self) -> tuple[str, str]:
        if not self.cfg.configured:
            return "idle", "Not set up yet: right-click, Settings"
        if not self.states:
            return "idle", "Checking the network drives…"
        good = sum(s.state == keeper.OK for s in self.states)
        if good == len(self.states):
            return "ok", f"{good} network drive{'s' if good != 1 else ''} connected"
        worst = next((s for s in self.states if s.state in (keeper.LOGIN, keeper.OFFLINE)), None)
        if worst and good == 0:
            return "bad", worst.message
        bad = next(s for s in self.states if s.state != keeper.OK)
        return "some", f"{good} of {len(self.states)} drives connected; {bad.drive.local} {bad.message}"

    def picture(self):
        from PIL import ImageDraw
        image = self.base.copy().resize((64, 64))
        ImageDraw.Draw(image).ellipse((38, 38, 63, 63), fill=DOTS[self.overall()[0]], outline=(255, 255, 255), width=3)
        return image

    def refresh(self) -> None:
        kind, text = self.overall()
        self.icon.icon = self.picture()
        self.icon.title = f"{TITLE}: {text}"[:127]
        self.icon.update_menu()
        if kind in ("bad", "some") and text != self.notified:
            self.notified = text
            try:
                self.icon.notify(text, TITLE)
            except Exception:
                pass
        elif kind == "ok":
            self.notified = ""

    def open_home(self) -> None:
        webbrowser.open(self.cfg.homepage)

    def reconnect(self) -> None:
        self.login_failed = False
        self.wake.set()

    def toggle_autostart(self) -> None:
        start_with_windows(not start_with_windows())
        self.icon.update_menu()

    def quit(self) -> None:
        log.info("quitting")
        self.uploader.stop()
        self.stopping = True
        self.wake.set()
        self.icon.stop()
        self.tasks.put(self.root.quit)

    # ---- settings (on the Tk thread) -----------------------------------------------------------

    def open_settings(self) -> None:
        from settings_window import SettingsWindow
        if self.settings and self.settings.alive():
            self.settings.lift()
            return
        self.settings = SettingsWindow(self.root, config.load(), self.saved, self.lock, RESOURCES / "icon.ico")

    def saved(self, cfg: config.Config, states) -> None:
        self.cfg, self.states, self.login_failed = cfg, states, False
        self.refresh()
        self.wake.set()

    def pump(self) -> None:
        while True:
            try:
                task = self.tasks.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception:
                log.exception("task failed")
        self.root.after(150, self.pump)

    # ---- uploads -------------------------------------------------------------------------------

    def uploads_label(self) -> str:
        jobs = self.uploader.snapshot()
        active = [j for j in jobs if j["state"] in uploader.ACTIVE]
        if active:
            return f"Uploads ({len(active)} sending)…"
        return "Uploads…"

    def uploads_changed(self) -> None:
        """From the uploader's thread: tell the person when an upload ends; keep the menu current."""
        for j in self.uploader.snapshot():
            before = self.job_states.get(j["id"])
            self.job_states[j["id"]] = j["state"]
            if before and before != j["state"] and j["state"] in ("done", "failed"):
                title = "Uploaded to Dodobe" if j["state"] == "done" else "Upload stopped"
                self.notify(f"{j['name']}: {j['message']}", title)
            if before not in (None, "waiting") and j["state"] == "waiting":
                self.notify(f"{j['name']}: the connection dropped; it carries on when Dodobe is back.", "Upload waiting")
        now = time.monotonic()
        if now - self.menu_at > 2:
            self.menu_at = now
            try:
                self.icon.title = f"{TITLE}: {self.overall()[1]}"[:127]
                self.icon.update_menu()
            except Exception:
                pass

    def pick_upload(self) -> None:
        from tkinter import filedialog
        folder = filedialog.askdirectory(parent=self._dialog_parent(), title="Choose a folder to upload to Dodobe",
                                         mustexist=True)
        if folder:
            self.start_upload(folder)

    def start_upload(self, folder: str, parent=None) -> None:
        from tkinter import messagebox
        parent = parent or self._dialog_parent()
        if not self.cfg.username:
            messagebox.showinfo(TITLE, "Set up your Dodobe login in Settings first.", parent=parent)
            return self.open_settings()
        dest = self.cfg.unc(self.cfg.dumpsite)
        try:
            items = uploader.scan(Path(folder))
        except OSError as exc:
            return messagebox.showerror(TITLE, f"Can't read {folder}: {exc}", parent=parent)
        total = sum(i.size for i in items)
        if not items:
            return messagebox.showinfo(TITLE, f"{Path(folder).name} has no files to upload.", parent=parent)
        from uploads_window import size
        if not messagebox.askyesno("Upload to Dodobe", f"Upload {Path(folder).name}: {len(items)} files, {size(total)}, "
                                   f"to {dest}\\{uploader.DROP}?\n\nEach file is checked on Dodobe; Dodobe imports the "
                                   "folder once it's all there.", parent=parent):
            return
        try:
            self.uploader.add(folder, dest)
        except uploader.Failed as exc:
            return messagebox.showerror(TITLE, str(exc), parent=parent)
        self.open_uploads()

    def open_uploads(self) -> None:
        from uploads_window import UploadsWindow
        if self.uploads_window and self.uploads_window.alive():
            return self.uploads_window.lift()
        self.uploads_window = UploadsWindow(self.root, self.uploader, self.start_upload, RESOURCES / "icon.ico")

    # ---- the box: wake, restart, shut down -----------------------------------------------------

    def learn(self) -> None:
        """While the box is on: note its address and network card, so it can be woken later."""
        if time.monotonic() - self.learned_at < LEARN_EVERY and config.load_box().get("mac"):
            return
        if not winnet.reachable(self.cfg.host):
            return
        found = winnet.box_address(self.cfg.host)
        self.learned_at = time.monotonic()
        if found:
            box = config.load_box()
            if (box.get("ip"), box.get("mac")) != found:
                config.save_box({"ip": found[0], "mac": found[1], "host": self.cfg.host})
                log.info("the box is %s, network card %s", *found)

    def wake_box(self) -> None:
        box = config.load_box()
        mac = config.normal_mac(self.cfg.mac) or box.get("mac")
        if not mac:
            return self.tasks.put(lambda: self._info(
                "The companion hasn't seen Dodobe's network card yet. Leave the box on while the companion runs "
                "for a minute, or type its network card (MAC) address in Settings."))
        if winnet.reachable(self.cfg.host, timeout=1.5):
            return self.notify("Dodobe is already on.")
        sent = wol.send(mac, box.get("ip"))
        log.info("wake-up sent to %s (%d packets)", mac, sent)
        self.notify(f"Wake-up sent to {mac}. Dodobe takes a minute or two to start.")

        def watch():
            deadline = time.monotonic() + WAKE_WAIT
            while time.monotonic() < deadline and not self.stopping:
                if winnet.reachable(self.cfg.host, timeout=2):
                    self.notify("Dodobe is awake.")
                    self.login_failed = False
                    self.wake.set()  # reconnect the drives now
                    return
                time.sleep(5)
            self.notify("Dodobe didn't wake up. It must be on a cable to this network, with wake-on-LAN allowed in "
                        "its BIOS (\"Wake on LAN\" or \"Power on by PCI-E\").")
        threading.Thread(target=watch, name="wake", daemon=True).start()

    def power(self, action: str) -> None:
        """On the Tk thread: confirm, get the password, then ask the studio on a thread."""
        from tkinter import messagebox, simpledialog
        parent = self._dialog_parent()
        what = "Restart" if action == "restart" else "Shut down"
        note = ("\n\nAn upload is going: it waits and carries on when Dodobe is back." if self.uploader.busy() else "")
        after = ("It's back in a few minutes." if action == "restart" else
                 "Wake it up again from this menu, or with its power button.")
        if not messagebox.askyesno(f"{what} Dodobe?", f"{what} the Dodobe box? Everyone using it is disconnected. "
                                   f"{after}{note}", icon="warning", parent=parent):
            return
        saved = winnet.studio_login(self.cfg.host)
        user = self.cfg.username
        password = saved[1] if saved and saved[0] == user else None
        if not user:
            messagebox.showinfo(TITLE, "Set up your Dodobe login in Settings first.", parent=parent)
            return
        if password is None:
            password = simpledialog.askstring(TITLE, f"Your Dodobe password ({user}), to {what.lower()} the box:",
                                              show="•", parent=parent)
            if not password:
                return
            ask_save = True
        else:
            ask_save = False

        def go():
            try:
                studio.power(self.cfg.studio, user, password, action)
            except studio.StudioError as exc:
                log.warning("%s refused: %s", action, exc)
                if "accept" in str(exc):
                    winnet.delete_studio_login(self.cfg.host)
                message = f"Couldn't {what.lower()} Dodobe. {exc}"
                self.tasks.put(lambda: self._info(message, error=True))
                return
            if ask_save:
                try:
                    winnet.save_studio_login(self.cfg.host, user, password)
                except winnet.NetError:
                    pass
            log.info("asked the box to %s", action)
            self.notify("Dodobe is restarting: back in a few minutes." if action == "restart" else
                        "Dodobe is shutting down.")
        threading.Thread(target=go, name="power", daemon=True).start()

    # ---- small helpers -------------------------------------------------------------------------

    def notify(self, text: str, title: str = TITLE) -> None:
        try:
            self.icon.notify(text[:250], title)
        except Exception:
            log.info("%s: %s", title, text)

    def _dialog_parent(self):
        """A hidden, always-on-top window, so a dialog opened from the tray comes to the front."""
        t = tk.Toplevel(self.root)
        t.withdraw()
        t.attributes("-topmost", True)
        self.root.after(120_000, t.destroy)
        return t

    def _info(self, text: str, error: bool = False) -> None:
        from tkinter import messagebox
        (messagebox.showerror if error else messagebox.showinfo)(TITLE, text, parent=self._dialog_parent())

    # ---- the watcher ---------------------------------------------------------------------------

    def watch(self) -> None:
        while not self.stopping:
            if not self.login_failed:
                with self.lock:  # a save in the settings window holds it too
                    cfg = self.cfg
                    try:
                        self.states = keeper.ensure(cfg)
                    except Exception:
                        log.exception("check failed")
                self.login_failed = any(s.state == keeper.LOGIN for s in self.states)
                if self.login_failed:
                    log.warning("the box refused the saved login: waiting for Settings or Reconnect now")
                self.refresh()
            self.learn()
            all_ok = self.states and all(s.state == keeper.OK for s in self.states)
            self.wake.wait(CHECK_OK if all_ok or not self.cfg.configured else CHECK_TROUBLE)
            self.wake.clear()

    def run(self) -> None:
        def ready(icon):  # the icon exists now: show it, then start checking the drives
            icon.visible = True
            threading.Thread(target=self.watch, name="watch", daemon=True).start()
        threading.Thread(target=self.icon.run, kwargs={"setup": ready}, name="tray", daemon=True).start()
        if not self.cfg.configured:
            self.tasks.put(self.open_settings)
        self.uploader.start()  # unfinished uploads carry on by themselves
        self.root.after(150, self.pump)
        self.root.mainloop()


def setup_logging() -> None:
    folder = config.data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / "companion.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    if sys.stdout is None or sys.stderr is None:  # pythonw and the frozen app have no console
        sys.stdout = sys.stderr = open(os.devnull, "w")


def main() -> None:
    setup_logging()
    if "--forget" in sys.argv:
        keeper.forget(config.load())
        log.info("forgot the drives and the saved login")
        return
    if not winnet.single_instance(APP):
        alert("The dodobe companion is already running: it's in the notification area by the clock.")
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # a sharp settings window on high-DPI screens
    except Exception:
        pass
    try:
        log.info("starting")
        App().run()
    except BaseException as exc:
        log.exception("stopped")
        alert(f"The dodobe companion stopped: {exc}\n\nSee companion.log in {config.data_dir()}")


if __name__ == "__main__":
    main()
