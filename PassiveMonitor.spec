# PyInstaller spec for the desktop build.
# Build with:  pyinstaller PassiveMonitor.spec --noconfirm
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("assets", "assets")]
binaries = []
hiddenimports = []

# Packages that ship data files (JS bundles, package metadata) PyInstaller
# does not pick up automatically.
for pkg in [
    "dash", "plotly", "dash_table", "dash_core_components",
    "dash_html_components", "webview", "waitress",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # package not present / nothing to collect
        print(f"[spec] skip collect_all({pkg}): {exc}")

# Our own code, imported dynamically via the page registry.
hiddenimports += collect_submodules("app")
# pywebview's Windows EdgeChromium backend talks to .NET through pythonnet.
hiddenimports += ["clr", "selenium", "webdriver_manager"]

a = Analysis(
    ["run_desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PassiveMonitor",
    console=False,          # no terminal window; errors go to unified_monitor.log
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="PassiveMonitor",
)
