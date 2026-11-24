"""dodobe companion: the Windows tray app.

Sits in the notification area from sign-in, opens the Dodobe homepage, and keeps Dodobe's network
folders on drive letters: Windows remembers them and restores them after a restart, and the
companion checks them every minute and reconnects any that dropped (the box was off, the PC woke
before the network).

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
import tkinter as tk
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FROZEN = getattr(sys, "frozen", False)
RESOURCES = Path(getattr(sys, "_MEIPASS", HERE))

import config  # noqa: E402
import keeper  # noqa: E402
import winnet  # noqa: E402

APP = "dodobeCompanion"
TITLE = "dodobe companion"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CHECK_OK, CHECK_TROUBLE = 60, 20  # seconds between checks: all well, something to fix
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

        self.root = tk.Tk()
        self.root.withdraw()
        icon = RESOURCES / "icon.png"
        self.base = Image.open(icon).convert("RGBA") if icon.exists() else Image.new("RGBA", (64, 64), (28, 51, 44, 255))
        item, menu = pystray.MenuItem, pystray.Menu
        self.icon = pystray.Icon(APP, self.picture(), TITLE, menu(
            item("Open Dodobe", self.open_home, default=True),
            item("Network drives", menu(self.drive_items)),
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
