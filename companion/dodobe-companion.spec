# PyInstaller spec for the dodobe companion (a folder build: dist\dodobe-companion).
# pystray (LGPL-3.0) is kept as plain .py files in _internal\pystray, so it can be replaced with
# another build of it, as its licence asks.
a = Analysis(
    ["dodobe_companion.pyw"],
    pathex=["."],
    datas=[("icon.png", "."), ("icon.ico", ".")],
    hiddenimports=["config", "keeper", "winnet", "settings_window", "uploads_window", "uploader", "studio", "wol",
                   "pystray._win32"],
    module_collection_mode={"pystray": "py"},
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="dodobe-companion", console=False, icon="icon.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="dodobe-companion")
