"""The settings window: the box's address and homepage, the network login, and which network
folders go on which drive letters. Slow work (finding folders, connecting) runs on a thread, and
its result comes back to the window through `after`.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk

import config
import keeper
import winnet
from config import Config, Drive

PAD = {"padx": 8, "pady": 4}


class SettingsWindow:
    def __init__(self, root: tk.Tk, cfg: Config, on_saved, lock: threading.Lock, icon_path=None):
        self.cfg, self.on_saved, self.lock = cfg, on_saved, lock
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self.rows: dict[str, dict] = {}  # share (lower case) -> {"name", "on", "letter", "combo"}

        self.win = w = tk.Toplevel(root)
        w.title("dodobe companion: settings")
        w.resizable(False, True)
        if icon_path and icon_path.exists():
            try:
                w.iconbitmap(str(icon_path))
            except tk.TclError:
                pass
        w.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(w, padding=12)
        frame.pack(fill="both", expand=True)

        box = ttk.LabelFrame(frame, text="Your Dodobe box", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.host = tk.StringVar(value=cfg.host)
        self.home = tk.StringVar(value=cfg.home_url)
        ttk.Label(box, text="Address").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.host, width=34).grid(row=0, column=1, sticky="we", **PAD)
        ttk.Label(box, text="Homepage").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.home, width=34).grid(row=1, column=1, sticky="we", **PAD)
        ttk.Label(box, text="Blank: http://<address>", foreground="gray").grid(row=2, column=1, sticky="w", padx=8)

        login = ttk.LabelFrame(frame, text="Your network login (your Dodobe name and password)", padding=8)
        login.pack(fill="x", pady=(0, 8))
        self.user = tk.StringVar(value=cfg.username)
        self.password = tk.StringVar()
        ttk.Label(login, text="Dodobe name").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(login, textvariable=self.user, width=34).grid(row=0, column=1, sticky="we", **PAD)
        ttk.Label(login, text="Password").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(login, textvariable=self.password, show="•", width=34).grid(row=1, column=1, sticky="we", **PAD)
        saved = cfg.username and _saved_user(cfg.host) == cfg.username
        ttk.Label(login, foreground="gray", text="Saved in Windows. Leave blank to keep it." if saved else
                  "Kept in Windows' Credential Manager, never in a file.").grid(row=2, column=1, sticky="w", padx=8)

        folders = ttk.LabelFrame(frame, text="Network folders as drives", padding=8)
        folders.pack(fill="both", expand=True, pady=(0, 8))
        top = ttk.Frame(folders)
        top.pack(fill="x")
        self.find_button = ttk.Button(top, text="Find folders", command=self.find)
        self.find_button.pack(side="left")
        ttk.Label(top, text="Tick the folders to keep as drives, and choose their letters.",
                  foreground="gray").pack(side="left", padx=8)
        self.list = ttk.Frame(folders)
        self.list.pack(fill="both", expand=True, pady=6)
        other = ttk.Frame(folders)
        other.pack(fill="x")
        self.other = tk.StringVar()
        ttk.Label(other, text="A hidden folder by name").pack(side="left")
        ttk.Entry(other, textvariable=self.other, width=18).pack(side="left", padx=6)
        ttk.Button(other, text="Add", command=self.add_other).pack(side="left")

        self.status = ttk.Label(frame, text="", wraplength=440, justify="left")
        self.status.pack(fill="x", pady=(0, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.save_button = ttk.Button(buttons, text="Save", command=self.save, default="active")
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right", padx=6)

        self.taken = _taken_letters(cfg)
        for d in cfg.drives:
            self.add_row(d.share, "", on=True, letter=d.letter)
        if not cfg.drives:
            ttk.Label(self.list, text="Press Find folders after entering your login.", foreground="gray").pack()
        w.after(100, self.poll)
        w.lift()
        w.focus_force()

    # ---- rows ----------------------------------------------------------------------------------

    def add_row(self, share: str, remark: str, on: bool, letter: str | None = None) -> None:
        key = share.lower()
        if key in self.rows:
            return
        if not self.rows:
            for child in self.list.winfo_children():
                child.destroy()
        chosen = {r["letter"].get() for r in self.rows.values() if r["on"].get()}
        letter = letter or config.suggest_letter(share, self.taken | chosen) or ""
        on_var, letter_var = tk.BooleanVar(value=on), tk.StringVar(value=letter)
        row = ttk.Frame(self.list)
        row.pack(fill="x")
        ttk.Checkbutton(row, variable=on_var, text=share).pack(side="left")
        if remark:
            ttk.Label(row, text=remark[:48], foreground="gray").pack(side="left", padx=6)
        free = [x for x in config.LETTERS if x not in self.taken]
        combo = ttk.Combobox(row, textvariable=letter_var, values=[f"{x}" for x in free], width=4, state="readonly")
        combo.pack(side="right")
        ttk.Label(row, text="Drive").pack(side="right", padx=4)
        self.rows[key] = {"name": share, "on": on_var, "letter": letter_var}

    def add_other(self) -> None:
        name = self.other.get().strip()
        if not config.SHARE.match(name):
            self.say("That isn't a network folder name Dodobe uses.")
            return
        self.add_row(name, "", on=True)
        self.other.set("")

    # ---- actions -------------------------------------------------------------------------------

    def read(self) -> Config:
        drives = [Drive(r["name"], r["letter"].get()) for r in self.rows.values() if r["on"].get()]
        return Config(host=self.host.get().strip(), home_url=self.home.get().strip(),
                      username=self.user.get().strip().lower(), drives=drives)

    def find(self) -> None:
        cfg = self.read()
        if not config.HOST.match(cfg.host):
            self.say("Enter the Dodobe box's address first (usually dodobe).")
            return
        self.run("Looking for network folders…", "found",
                 lambda: keeper.find_shares(cfg.host, cfg.username, self.password.get() or None))

    def save(self) -> None:
        new = self.read()
        problems = config.validate(new)
        if new.drives and not new.username:
            problems.append("Enter your Dodobe name: the network folders open with your Dodobe login.")
        if problems:
            self.say("\n".join(problems))
            return
        old, password = self.cfg, self.password.get() or None

        def work():
            with self.lock:  # not while the tray app is checking the drives
                config.save(new)
                return new, keeper.apply(old, new, password)
        self.run("Connecting…", "saved", work)

    def run(self, message: str, kind: str, job) -> None:
        if self.busy:
            return
        self.busy = True
        self.say(message)
        for b in (self.find_button, self.save_button):
            b.state(["disabled"])

        def thread():
            try:
                self.results.put((kind, job()))
            except Exception as exc:  # NetError and anything unexpected: shown in the window
                self.results.put(("error", exc))
        threading.Thread(target=thread, daemon=True).start()

    def poll(self) -> None:
        try:
            kind, value = self.results.get_nowait()
        except queue.Empty:
            self.win.after(100, self.poll)
            return
        self.busy = False
        for b in (self.find_button, self.save_button):
            b.state(["!disabled"])
        if kind == "found":
            for name, remark in value:
                self.add_row(name, remark, on=False)
            self.say(f"Found {len(value)} network folder{'s' if len(value) != 1 else ''}." if value else
                     "The box shows no network folders to this login.")
        elif kind == "saved":
            new, states = value
            self.cfg = new
            self.password.set("")
            self.on_saved(new, states)
            bad = [s for s in states if s.state != keeper.OK]
            if bad:
                self.say("Saved, but:\n" + "\n".join(f"{s.drive.local} {s.drive.share}: {s.message}" for s in bad))
            else:
                self.close()
                return
        else:
            self.say(str(value))
        self.win.after(100, self.poll)

    def say(self, text: str) -> None:
        self.status.configure(text=text)

    def close(self) -> None:
        if self.busy:  # a save half done must still reach the tray app
            self.say("Still working: one moment…")
            return
        self.win.destroy()

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def lift(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()


def _saved_user(host: str) -> str | None:
    try:
        return winnet.credential_user(host)
    except Exception:
        return None


def _taken_letters(cfg: Config) -> set[str]:
    """Letters used by disks or other mappings; the companion's own stay choosable."""
    mine = {d.letter for d in cfg.drives}
    try:
        return winnet.used_letters() - mine
    except Exception:
        return set()
