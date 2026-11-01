# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller build for Windows, Linux and macOS. Run through packaging/build.py, which sets
# LIBUSB_PATH to the libusb library to bundle.
#
#   Windows: dist/lightning-monitor-bridge/  lightning-bridge.exe (console) + "Lightning Monitor Bridge.exe" (tray)
#   Linux:   dist/lightning-bridge           one file; "lightning-bridge tray" starts the tray app
#   macOS:   dist/Lightning Monitor Bridge.app  menu-bar app; its executable also takes the commands

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "src"))
from lightning_bridge import APP_NAME, __version__  # noqa: E402

icons = root / "build" / "icons"
libusb = os.environ.get("LIBUSB_PATH")
binaries = [(libusb, ".")] if libusb else []
backend = {"win32": "pystray._win32", "darwin": "pystray._darwin"}.get(sys.platform, "pystray._xorg")
hidden = [backend, "usb.backend.libusb1", "serial.tools.list_ports"] + collect_submodules("zeroconf")
if sys.platform == "darwin":
    hidden += ["PyObjCTools.AppHelper"]

analysis = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    hiddenimports=hidden,
    excludes=["tkinter", "unittest", "pytest"],
)
pyz = PYZ(analysis.pure)

if sys.platform == "win32":
    console = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="lightning-bridge",
                  console=True, icon=str(icons / "bridge.ico"))
    tray = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=APP_NAME,
               console=False, icon=str(icons / "bridge.ico"))
    COLLECT(console, tray, analysis.binaries, analysis.datas, name="lightning-monitor-bridge")
elif sys.platform == "darwin":
    app = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=APP_NAME, console=False,
              argv_emulation=False, codesign_identity=os.environ.get("CODESIGN_IDENTITY"),
              entitlements_file=str(root / "packaging" / "macos" / "entitlements.plist"))
    collected = COLLECT(app, analysis.binaries, analysis.datas, name=APP_NAME)
    BUNDLE(
        collected,
        name="%s.app" % APP_NAME,
        icon=str(icons / "bridge.icns"),
        bundle_identifier="au.com.brien.lightning-monitor-bridge",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSUIElement": True,
            "LSMinimumSystemVersion": os.environ.get("MACOSX_DEPLOYMENT_TARGET", "14.0"),
            "NSHumanReadableCopyright": "GPL-3.0-or-later",
            "NSLocalNetworkUsageDescription": "Lightning Monitor on your other devices connects to this bridge to read the lightning detector.",
            "NSBonjourServices": ["_lightningmon._tcp"],
        },
    )
else:
    EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, name="lightning-bridge", console=True)
