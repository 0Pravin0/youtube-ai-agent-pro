# PyInstaller recipe for the local engine — macOS arm64, one-folder build (fast start, no unpacking).
# Also serves the built-in MCP servers: `yta-engine --mcp-server <name>`.
# Build:  pyinstaller --noconfirm yta-engine.spec      -> dist/yta-engine/
# FFmpeg: put ffmpeg(.exe) and ffprobe(.exe) in dist/yta-engine/bin/ afterwards.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (
    collect_submodules("app")
    + collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))  # CLI extra not used
    + collect_submodules("mcp_types")
    + ["sse_starlette", "multipart", "python_multipart", "tzdata", "httpx2"]
)
datas = (
    [("app/assets", "app/assets")]
    + collect_data_files("googleapiclient")   # offline API discovery documents (YouTube, Analytics)
    + collect_data_files("tzdata")            # time zones (bundled, independent of the OS)
    + collect_data_files("certifi")
    + collect_data_files("anthropic")
    + collect_data_files("mcp")
    + [("deps", "deps"), ("app/workers/parler_worker.py", "app/workers")]  # pinned versions + checksums of the free tools (deps.lock.json, hashed requirements)
)

a = Analysis(
    ["sidecar.py"],
    pathex=["."],
    hiddenimports=hidden,
    datas=datas,
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy.tests", "win32ctypes"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yta-engine",
    console=True,
    target_arch="arm64",  # Apple Silicon only
    icon=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="yta-engine", upx=False)
