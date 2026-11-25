"""The Uploads window: each folder being sent to Dodobe, with its progress, and Pause, Resume,
Cancel. It only shows the uploader's state (a snapshot every half second); the work is on its thread.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

STATES = {"queued": "Waiting its turn", "copying": "Sending", "waiting": "Waiting for Dodobe", "verifying": "Checking",
          "moving": "Putting it in Drop here", "paused": "Paused", "done": "Done", "failed": "Stopped",
          "cancelled": "Cancelled"}


def size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class UploadsWindow:
    def __init__(self, root: tk.Tk, uploader, start_upload, icon_path=None):
        self.uploader, self.start_upload = uploader, start_upload
        self.win = w = tk.Toplevel(root)
        w.title("dodobe companion: uploads")
        w.minsize(560, 200)
        if icon_path and icon_path.exists():
            try:
                w.iconbitmap(str(icon_path))
            except tk.TclError:
                pass
        top = ttk.Frame(w, padding=(12, 12, 12, 0))
        top.pack(fill="x")
        ttk.Button(top, text="Upload a folder…", command=self.pick).pack(side="left")
        ttk.Label(top, foreground="gray", wraplength=380, justify="left",
                  text="Each file is checked on Dodobe before the folder goes into Drop here. "
                       "If the network drops, it waits and carries on.").pack(side="left", padx=10)
        self.body = ttk.Frame(w, padding=12)
        self.body.pack(fill="both", expand=True)
        self.rows: dict[str, dict] = {}
        self.refresh()

    def pick(self) -> None:
        folder = filedialog.askdirectory(parent=self.win, title="Choose a folder to upload to Dodobe", mustexist=True)
        if folder:
            self.start_upload(folder, self.win)

    def row(self, job_id: str) -> dict:
        if job_id in self.rows:
            return self.rows[job_id]
        f = ttk.Frame(self.body, padding=(0, 4))
        f.pack(fill="x")
        title = ttk.Label(f, font=("Segoe UI", 10, "bold"))
        title.grid(row=0, column=0, sticky="w")
        bar = ttk.Progressbar(f, length=360, maximum=1000)
        bar.grid(row=1, column=0, sticky="we", pady=2)
        text = ttk.Label(f, foreground="gray", wraplength=360, justify="left")
        text.grid(row=2, column=0, sticky="w")
        buttons = ttk.Frame(f)
        buttons.grid(row=0, column=1, rowspan=3, sticky="e", padx=(12, 0))
        f.columnconfigure(0, weight=1)
        r = {"frame": f, "title": title, "bar": bar, "text": text, "buttons": buttons, "kind": None}
        self.rows[job_id] = r
        return r

    def set_buttons(self, r: dict, job: dict) -> None:
        kind = ("pause" if job["state"] in ("queued", "copying", "waiting", "verifying") else
                "resume" if job["state"] in ("paused", "failed") else "clear" if job["state"] in ("done", "cancelled") else "none")
        if r["kind"] == kind:
            return
        for b in r["buttons"].winfo_children():
            b.destroy()
        jid = job["id"]
        if kind == "pause":
            ttk.Button(r["buttons"], text="Pause", command=lambda: self.uploader.pause(jid)).pack(side="left")
        if kind == "resume":
            ttk.Button(r["buttons"], text="Resume", command=lambda: self.uploader.resume(jid)).pack(side="left")
        if kind in ("pause", "resume"):
            ttk.Button(r["buttons"], text="Cancel", command=lambda: self.cancel(job)).pack(side="left", padx=4)
        if kind == "clear":
            ttk.Button(r["buttons"], text="Clear", command=lambda: self.uploader.forget(jid)).pack(side="left")
        r["kind"] = kind

    def cancel(self, job: dict) -> None:
        if messagebox.askyesno("Cancel the upload?", f"Stop sending {job['name']}? What was sent so far is removed "
                               "from Dodobe; nothing goes into Drop here.", parent=self.win):
            self.uploader.cancel(job["id"])

    def refresh(self) -> None:
        if not self.alive():
            return
        jobs = self.uploader.snapshot()
        seen = set()
        for job in reversed(jobs):  # newest first
            seen.add(job["id"])
            r = self.row(job["id"])
            r["title"].configure(text=job["name"])
            r["bar"]["value"] = 1000 * job["done"] / job["total"] if job["total"] else 1000
            speed = f" · {size(job['rate'])}/s" if job["state"] == "copying" and job["rate"] else ""
            r["text"].configure(text=f"{STATES.get(job['state'], job['state'])}: {job['files_done']} of {job['files']} "
                                     f"files, {size(job['done'])} of {size(job['total'])}{speed}\n{job['message']}")
            self.set_buttons(r, job)
        for jid in [j for j in self.rows if j not in seen]:
            self.rows.pop(jid)["frame"].destroy()
        if not jobs and not self.body.winfo_children():
            ttk.Label(self.body, text="Nothing uploaded yet.", foreground="gray").pack()
        elif jobs:
            for child in self.body.winfo_children():
                if isinstance(child, ttk.Label):
                    child.destroy()
        self.win.after(500, self.refresh)

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def lift(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
