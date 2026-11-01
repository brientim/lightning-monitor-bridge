# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the download for this computer's platform into dist/release/.

  python packaging/build.py            # Windows (32-bit Python), Linux or macOS
  LIBUSB_PATH=... python packaging/build.py   # use a specific libusb library

Asset names stay the same for every version so https://github.com/brientim/lightning-monitor-bridge/
releases/latest/download/<asset> always points at the newest build. On macOS, run
packaging/macos/sign-and-notarize.sh afterwards to sign, notarise and zip the app.
"""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
RELEASE = DIST / "release"


def find_libusb() -> str | None:
    if os.environ.get("LIBUSB_PATH"):
        return os.environ["LIBUSB_PATH"]
    if sys.platform == "darwin":
        prefix = subprocess.run(["brew", "--prefix", "libusb"], capture_output=True, text=True).stdout.strip()
        candidate = Path(prefix) / "lib" / "libusb-1.0.0.dylib"
        return str(candidate.resolve()) if candidate.exists() else None
    if sys.platform.startswith("linux"):
        name = ctypes.util.find_library("usb-1.0")
        if name:
            listing = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
            for line in listing.splitlines():
                if line.strip().startswith(name) and "=>" in line:
                    return str(Path(line.split("=>", 1)[1].strip()).resolve())
    return None


def linux_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def main() -> None:
    if sys.platform == "win32" and struct.calcsize("P") != 4:
        sys.exit("Build Windows with 32-bit Python, so Boltek's 32-bit LD350_DLL.dll can be used")
    libusb = find_libusb()
    if not libusb:
        sys.exit("libusb not found; set LIBUSB_PATH")
    env = dict(os.environ, LIBUSB_PATH=libusb)
    print("Bundling libusb from", libusb)

    subprocess.run([sys.executable, str(ROOT / "packaging" / "make_icons.py")], check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
         str(ROOT / "packaging" / "lightning-bridge.spec")],
        check=True, env=env,
    )
    RELEASE.mkdir(parents=True, exist_ok=True)
    extras = [ROOT / "LICENSE", ROOT / "README.md"]

    if sys.platform == "win32":
        folder = DIST / "lightning-monitor-bridge"
        for extra in extras + [ROOT / "packaging" / "windows" / "README-Windows.txt"]:
            shutil.copy(extra, folder / extra.name)
        archive = RELEASE / "lightning-monitor-bridge-windows-x86.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(folder.rglob("*")):
                zipped.write(path, Path("lightning-monitor-bridge") / path.relative_to(folder))
    elif sys.platform == "darwin":
        print("Built %s; now run packaging/macos/sign-and-notarize.sh" % (DIST / "Lightning Monitor Bridge.app"))
        return
    else:
        archive = RELEASE / ("lightning-monitor-bridge-linux-%s.tar.gz" % linux_arch())
        with tarfile.open(archive, "w:gz") as tarred:
            base = "lightning-monitor-bridge"
            tarred.add(DIST / "lightning-bridge", "%s/lightning-bridge" % base)
            for extra in extras + [ROOT / "packaging" / "linux" / "install.sh", ROOT / "packaging" / "linux" / "99-boltek-ld350.rules"]:
                tarred.add(extra, "%s/%s" % (base, extra.name))
    print("Wrote", archive)


if __name__ == "__main__":
    main()
