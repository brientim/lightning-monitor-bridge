# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the download for this computer's platform into dist/release/.

  python packaging/build.py            # Windows (32-bit Python), Linux or macOS
  LIBUSB_PATH=... python packaging/build.py   # use a specific libusb library
  python packaging/build.py --no-archive      # Windows: build dist/lightning-monitor-bridge/ only (to sign it)
  python packaging/build.py --archive DIR     # Windows: zip a (signed) build folder

On Windows the NexStorm connector (nexstorm/LD350_DLL.dll) is built too, with 32-bit MSVC (an x86 Developer
Command Prompt, or ilammy/msvc-dev-cmd in CI) or i686-w64-mingw32-gcc; LD350_CONNECTOR_DLL uses a prebuilt one.

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
SETUP_NAME = "NexStorm Connector Setup.exe"
#: Also a release asset of its own, for a PC that only runs NexStorm.
SETUP_ASSET = "nexstorm-connector-setup.exe"


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


def build_nexstorm_connector(output: Path) -> None:
    """Builds the 32-bit NexStorm connector, a stand-in for Boltek's LD350_DLL.dll, as ``output``."""
    sys.path.insert(0, str(ROOT / "src"))
    from lightning_bridge import __version__
    from lightning_bridge.nexstorm import CONNECTOR_MARKER

    source = ROOT / "nexstorm" / "ld350_connector.c"
    exports = ROOT / "nexstorm" / "ld350_connector.def"
    work = ROOT / "build" / "nexstorm"
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    version = '/DCONNECTOR_VERSION="%s"' % __version__
    if os.environ.get("LD350_CONNECTOR_DLL"):
        shutil.copyfile(os.environ["LD350_CONNECTOR_DLL"], output)
    elif shutil.which("cl"):
        subprocess.run(
            ["cl", "/nologo", "/O2", "/MT", "/W3", "/LD", version, str(source), "/Fo%s\\" % work, "/Fe%s" % output,
             "/link", "/MACHINE:X86", "/DEF:%s" % exports, "/IMPLIB:%s" % (work / "LD350_DLL.lib"), "ws2_32.lib"],
            check=True, cwd=work,
        )
    elif shutil.which("i686-w64-mingw32-gcc"):
        subprocess.run(
            ["i686-w64-mingw32-gcc", "-std=c11", "-Os", "-s", "-Wall", "-shared", "-static-libgcc", "-D" + version[2:],
             "-o", str(output), str(source), str(exports), "-lws2_32"],
            check=True,
        )
    else:
        sys.exit("The NexStorm connector needs 32-bit MSVC (an x86 Developer Command Prompt) or i686-w64-mingw32-gcc, "
                 "or set LD350_CONNECTOR_DLL")
    if CONNECTOR_MARKER not in output.read_bytes():
        sys.exit("%s is not the NexStorm connector" % output)
    print("Built the NexStorm connector", output)


def build_nexstorm_setup(connector: Path, output: Path) -> None:
    """Builds NexStorm Connector Setup.exe, which carries ``connector`` and installs it into NexStorm's folder."""
    sys.path.insert(0, str(ROOT / "src"))
    from lightning_bridge import __version__

    work = ROOT / "build" / "nexstorm-setup"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("connector_setup.rc", "connector_setup.manifest"):
        shutil.copyfile(ROOT / "nexstorm" / name, work / name)
    shutil.copyfile(connector, work / "LD350_DLL.dll")
    source = ROOT / "nexstorm" / "connector_setup.c"
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("cl") and shutil.which("rc"):
        subprocess.run(["rc", "/nologo", '/dCONNECTOR_VERSION_TEXT="%s"' % __version__, "/fo", "setup.res", "connector_setup.rc"],
                       check=True, cwd=work)
        subprocess.run(
            ["cl", "/nologo", "/O2", "/MT", "/W3", '/DCONNECTOR_VERSION="%s"' % __version__, str(source), "setup.res",
             "/Fo%s\\" % work, "/Fe%s" % output, "/link", "/MACHINE:X86", "/MANIFEST:NO",
             "advapi32.lib", "shell32.lib", "user32.lib"],
            check=True, cwd=work,
        )
    elif shutil.which("i686-w64-mingw32-gcc"):
        subprocess.run(["i686-w64-mingw32-windres", '-DCONNECTOR_VERSION_TEXT=\\"%s\\"' % __version__,
                        "connector_setup.rc", "-O", "coff", "-o", "setup.res"], check=True, cwd=work)
        subprocess.run(
            ["i686-w64-mingw32-gcc", "-std=c11", "-Os", "-s", "-Wall", "-static", '-DCONNECTOR_VERSION="%s"' % __version__,
             "-o", str(output), str(source), str(work / "setup.res"), "-ladvapi32", "-lshell32", "-luser32"],
            check=True,
        )
    else:
        sys.exit("NexStorm Connector Setup needs 32-bit MSVC (cl and rc) or i686-w64-mingw32-gcc")
    print("Built", output)


def archive_windows(folder: Path) -> Path:
    RELEASE.mkdir(parents=True, exist_ok=True)
    archive = RELEASE / "lightning-monitor-bridge-windows-x86.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(folder.rglob("*")):
            zipped.write(path, Path("lightning-monitor-bridge") / path.relative_to(folder))
    print("Wrote", archive)
    setup = folder / "nexstorm" / SETUP_NAME
    if setup.exists():
        shutil.copyfile(setup, RELEASE / SETUP_ASSET)
        print("Wrote", RELEASE / SETUP_ASSET)
    return archive


def main() -> None:
    if "--archive" in sys.argv:
        archive_windows(Path(sys.argv[sys.argv.index("--archive") + 1]))
        return
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
        build_nexstorm_connector(folder / "nexstorm" / "LD350_DLL.dll")
        build_nexstorm_setup(folder / "nexstorm" / "LD350_DLL.dll", folder / "nexstorm" / SETUP_NAME)
        if "--no-archive" in sys.argv:
            print("Built", folder)
        else:
            archive_windows(folder)
        return
    if sys.platform == "darwin":
        print("Built %s; now run packaging/macos/sign-and-notarize.sh" % (DIST / "Lightning Monitor Bridge.app"))
        return
    archive = RELEASE / ("lightning-monitor-bridge-linux-%s.tar.gz" % linux_arch())
    with tarfile.open(archive, "w:gz") as tarred:
        base = "lightning-monitor-bridge"
        tarred.add(DIST / "lightning-bridge", "%s/lightning-bridge" % base)
        for extra in extras + [ROOT / "packaging" / "linux" / "install.sh", ROOT / "packaging" / "linux" / "99-boltek-ld350.rules"]:
            tarred.add(extra, "%s/%s" % (base, extra.name))
    print("Wrote", archive)


if __name__ == "__main__":
    main()
