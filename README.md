# Lightning Monitor Bridge

Shares a **Boltek LD-350** (USB) or **LD-250** (serial) lightning detector on your network, so
[Lightning Monitor](https://brien.com.au) on a Mac, iPhone or iPad can use a detector plugged into
another computer — a Windows PC, a Linux box or a Raspberry Pi.

It's optional: if the detector is plugged into the Mac that runs Lightning Monitor, you don't need it.

- Windows, Linux (x86_64 and arm64, including Raspberry Pi) and macOS
- LD-350 over USB, with waveforms (so Lightning Monitor can classify strikes), or through Boltek's own
  Windows driver so NexStorm keeps working
- LD-250 / EFM-100 through a USB-serial adapter
- Appears in Lightning Monitor under **Settings › Detector › On this network** (Bonjour)
- Up to 16 devices at once, with an optional token
- A tray / menu-bar app, or a headless service that starts at boot

## Download

Also on [brien.com.au](https://brien.com.au/apps/lightning-monitor-bridge.html).

| System | Download |
|---|---|
| Windows 10 / 11 | [lightning-monitor-bridge-windows-x86.zip](https://github.com/brientim/lightning-monitor-bridge/releases/latest/download/lightning-monitor-bridge-windows-x86.zip) |
| Linux x86_64 (Ubuntu 22.04, Debian 12 or later) | [lightning-monitor-bridge-linux-x86_64.tar.gz](https://github.com/brientim/lightning-monitor-bridge/releases/latest/download/lightning-monitor-bridge-linux-x86_64.tar.gz) |
| Linux arm64 (Raspberry Pi 4 / 5 with 64-bit Raspberry Pi OS Bookworm or later) | [lightning-monitor-bridge-linux-arm64.tar.gz](https://github.com/brientim/lightning-monitor-bridge/releases/latest/download/lightning-monitor-bridge-linux-arm64.tar.gz) |
| macOS 14 or later, Apple silicon | [lightning-monitor-bridge-macos-arm64.zip](https://github.com/brientim/lightning-monitor-bridge/releases/latest/download/lightning-monitor-bridge-macos-arm64.zip) |

Other systems (32-bit or older Raspberry Pi OS, Intel Macs): [install with Python](#install-with-python).

Only one program can open the detector at a time: quit NexStorm, StormVue or Lightning Monitor on that
computer first.

## Windows

1. Unzip, and double-click **Lightning Monitor Bridge.exe**. A bolt appears in the notification area
   (yellow when the detector is connected). Right-click it for status, **Settings…**, **Open at Login**
   and **Quit**.
2. Allow it through Windows Defender Firewall on private networks when asked.
3. Choose how it reads the LD-350 (`ld350_driver` in Settings…; see `README-Windows.txt`):
   - **WinUSB** (waveforms): install WinUSB for *LD-350 Lightning Detector (Interface 0)* with
     [Zadig](https://zadig.akeo.ie). NexStorm can't use the detector while WinUSB is installed.
   - **Boltek's driver** (NexStorm keeps working, no waveforms): put `LD350_DLL.dll` from Boltek's
     [LD-350 Programmers Toolkit](https://downloads.boltek.com/LD350_DLL.zip) next to the bridge.
     It needs the Visual C++ 2013 (x86) runtime.
   - `auto` (default) uses WinUSB when it's installed, otherwise Boltek's driver.

The download isn't code-signed for Windows yet, so SmartScreen may warn: choose **More info › Run anyway**.

## Linux and Raspberry Pi

```bash
tar xzf lightning-monitor-bridge-linux-arm64.tar.gz
cd lightning-monitor-bridge
sudo ./install.sh                       # LD-350 on USB
sudo ./install.sh --ld250 /dev/ttyUSB0  # or an LD-250
```

`install.sh` copies `lightning-bridge` to `/usr/local/bin`, adds a udev rule so the LD-350 can be opened
without root, and starts a systemd service (`lightning-monitor-bridge`) that runs at boot as your user.
It needs `libusb-1.0-0` (installed on Raspberry Pi OS and Ubuntu). `lightning-bridge tray` starts the
tray icon on a desktop.

```bash
systemctl status lightning-monitor-bridge
journalctl -u lightning-monitor-bridge -f
```

## macOS

Unzip and open **Lightning Monitor Bridge**; a bolt appears in the menu bar. It's signed and notarised.
The same app runs headless from Terminal:

```bash
"/Applications/Lightning Monitor Bridge.app/Contents/MacOS/Lightning Monitor Bridge" run
```

## Connect from Lightning Monitor

In Lightning Monitor: **Settings › Detector › Connect via › Shared detector on the network**, then pick the
bridge under **On this network** (it shows the computer's name). If it doesn't appear — for example on
another subnet — enter the host and port shown in the bridge's menu (default port 10001).

## Settings

Settings are a JSON file (`lightning-bridge config path` shows where):

| Setting | Default | |
|---|---|---|
| `detector` | `ld350` | `ld350` (USB) or `ld250` (serial) |
| `serial_port` | | LD-250 port: `COM3`, `/dev/ttyUSB0`, `/dev/cu.usbserial-…` |
| `ld350_driver` | `auto` | Windows: `auto`, `libusb` (WinUSB) or `boltek` |
| `port` | `10001` | TCP port |
| `host` | `0.0.0.0` | Address to listen on |
| `token` | | Clients must send it; empty lets anyone on the network connect |
| `advertise` | `true` | Announce with Bonjour |
| `name` | computer name | Name shown in Lightning Monitor |
| `libusb`, `boltek_dll` | | Library paths, if not found automatically |

```bash
lightning-bridge devices                      # detectors and serial ports this computer can see
lightning-bridge config                       # show settings
lightning-bridge config set detector ld250
lightning-bridge config set serial_port COM3
lightning-bridge token --new                  # require a token; enter it in Lightning Monitor
lightning-bridge run                          # share in the foreground (Ctrl+C stops)
lightning-bridge install-service              # start automatically (--tray for the tray app, --system at boot)
lightning-bridge uninstall-service
```

Command-line options override the file for one run (`run --ld250 COM3 --port 10002 --token …`), and the
token can also come from `LIGHTNING_BRIDGE_TOKEN`. The old `ld_proxy.py` options still work.

## Security

The bridge is meant for your own network. Without a token, anyone on the network can read the detector
and change its settings. With a token, clients must authenticate first, but the connection is not
encrypted. Don't expose the port to the internet.

## Protocol

The same as Lightning Monitor's own sharing: everything the detector sends is relayed byte for byte;
clients send command lines (`VER`, `SQ 49` …), and `KA` / `STATUS` are dropped because the bridge keeps
the detector alive itself. With a token, a client sends `AUTH <token>` within 5 seconds. After
authenticating it receives `:SERVER Lightning Monitor Bridge LD-350`. Lines over 256 bytes get
`:ERROR line too long`; a client more than 1 MB behind is disconnected. Bonjour: service type
`_lightningmon._tcp`, TXT `server`, `host`, `port`, `auth`.

## Install with Python

```bash
python3 -m pip install "lightning-monitor-bridge[tray] @ git+https://github.com/brientim/lightning-monitor-bridge"
lightning-bridge devices
```

macOS needs `brew install libusb`. Linux needs the udev rule in `packaging/linux/`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[tray,dev]"
pytest
python packaging/build.py          # this platform's download into dist/release/
```

Pushing a `v*` tag builds the Windows and Linux downloads on GitHub Actions and attaches them to the
release. The macOS download is signed and notarised with `packaging/macos/sign-and-notarize.sh`.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE). Boltek, LD-350, LD-250, NexStorm and StormVue are trademarks of
their owners; this project is not affiliated with Boltek.
