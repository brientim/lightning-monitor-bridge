# Lightning Monitor Bridge

Shares a **Boltek LD-350** (USB) or **LD-250** (serial) lightning detector on your network, so
[Lightning Monitor](https://brien.com.au) on a Mac, iPhone or iPad can use a detector plugged into
another computer — a Windows PC, a Linux box or a Raspberry Pi.

It's optional: if the detector is plugged into the Mac that runs Lightning Monitor, you don't need it.

- Windows, Linux (x86_64 and arm64, including Raspberry Pi) and macOS
- LD-350 over USB, with waveforms (so Lightning Monitor can classify strikes), including through
  Boltek's own Windows driver, so NexStorm can use the detector again whenever the bridge is closed
- LD-250 / EFM-100 through a USB-serial adapter
- **NexStorm on a Windows PC can read an LD-350 plugged into a Mac**, at the same time as Lightning
  Monitor ([NexStorm](#nexstorm-reading-a-detector-on-a-mac))
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
computer first. NexStorm on the same PC can still use the detector through the bridge
([NexStorm](#nexstorm-reading-a-detector-on-a-mac)).

## Windows

1. Unzip, and double-click **Lightning Monitor Bridge.exe**. A bolt appears in the notification area
   (yellow when the detector is connected). Right-click it for status, **Settings…**, **Open at Login**
   and **Quit**.
2. Allow it through Windows Defender Firewall on private networks when asked.
3. Choose how it reads the LD-350 (`ld350_driver` in Settings…; see `README-Windows.txt`):
   - **WinUSB**: install WinUSB for *LD-350 Lightning Detector (Interface 0)* with
     [Zadig](https://zadig.akeo.ie). NexStorm can't use the detector while WinUSB is installed.
   - **Boltek's driver** (NexStorm can use the detector again when the bridge is closed): put `LD350_DLL.dll` from Boltek's
     [LD-350 Programmers Toolkit](https://downloads.boltek.com/LD350_DLL.zip) next to the bridge.
     It needs the Visual C++ 2013 (x86) runtime. The DLL hands over decoded waveforms, which the
     bridge packs back into the detector's own format, so strikes are still classified (not yet
     tried with a detector).
   - `auto` (default) uses WinUSB when it's installed, otherwise Boltek's driver.

Until the Windows download is code-signed (see [Code signing policy](#code-signing-policy)), SmartScreen may warn: choose **More info › Run anyway**. A newly signed program can still be warned about until it has built up download reputation.

## NexStorm reading a detector on a Mac

The other way round: the LD-350 stays on the Mac's USB, Lightning Monitor shares it, and NexStorm on a
Windows PC reads it from the network. Lightning Monitor, NexStorm and up to 15 other viewers use the
detector at once.

The same connector also works with the detector on **this PC**: the bridge opens it (WinUSB, or Boltek's
toolkit DLL beside the bridge) and shares it, and NexStorm reads it through the bridge like any other
viewer, so NexStorm and Lightning Monitor on the Mac use it at the same time. Choose **NexStorm › Read the
Detector From › This PC's Detector** (or `lightning-bridge nexstorm connect this-pc`) and keep **Sharing**
on. NexStorm itself no longer opens the USB, so the WinUSB driver is fine. The bridge never loads the
connector as Boltek's DLL; for the `boltek` driver, put Boltek's own `LD350_DLL.dll` (from the toolkit, or
a copy of NexStorm's `LD350_DLL.dll.boltek` renamed) next to the bridge.

| Detector on | Who opens the USB | NexStorm reads | Lightning Monitor reads |
|---|---|---|---|
| The Mac | Lightning Monitor | the Mac (`nexstorm connect My-Mac.local`) | its own USB |
| A Windows PC | the bridge | this PC (`nexstorm connect this-pc`) | the bridge, under **On this network** |

NexStorm opens the detector through Boltek's `LD350_DLL.dll`. The bridge comes with the **Lightning
Monitor NexStorm Connector** (`nexstorm\LD350_DLL.dll`), which has the same functions but reads the
Mac's shared detector instead of USB. Setting it up puts it in NexStorm's folder and keeps Boltek's
copies beside it as `LD350_DLL.dll.boltek`, so they can be put back.

**Simplest:** run **NexStorm Connector Setup.exe** (in the download's `nexstorm` folder, or on its own as
`nexstorm-connector-setup.exe`) and choose **1**. It asks for the Mac's name, port and token, tests the
connection with the connector itself, closes NexStorm, installs the connector (keeping Boltek's files) and starts
NexStorm again; **3** restores Boltek's driver. For scripts: `/install /host=… /token=… [/port=] [/allow-settings]
[/folder=] [/no-start]`, `/restore`, `/status`. It writes the same settings file as the bridge.

Or with the bridge's tray app:

1. On the Mac, in Lightning Monitor: **Settings › Detector › Sharing › Share this detector on the network**.
   Note the token if you generate one.
2. On the PC, quit NexStorm and start the bridge. Right-click the bolt › **NexStorm**:
   - **Read the Detector From** › the Mac. If it isn't listed, or the Mac uses a token, open
     **Settings…** and set `nexstorm_host` (e.g. `My-Mac.local` or its IP address) and `nexstorm_token`,
     then **Reload Settings**.
   - **Set Up NexStorm**. Windows asks for administrator rights when NexStorm is in Program Files.
3. Start NexStorm. It finds the "receiver" as usual; strikes, status and waveforms come from the Mac.

**Restore Boltek's Driver** puts NexStorm back on this PC's USB. The same from the command line:

```bat
lightning-bridge nexstorm connect My-Mac.local --token TOKEN
lightning-bridge nexstorm install            (as administrator if NexStorm is in Program Files)
lightning-bridge nexstorm status
lightning-bridge nexstorm restore
```

How it behaves:

- NexStorm's own detector-setting changes (squelch / trigger level, alarms, `SAVE`, force trigger) would
  change the detector for every viewer, so they're not sent unless you turn on **Let NexStorm Change
  Detector Settings** (`nexstorm_allow_settings`, or `--allow-settings`). Queries such as `SQ` or `VER`
  always go through. NexStorm turning waveforms off only stops them reaching NexStorm; they stay on for
  Lightning Monitor.
- Lightning Monitor keeps the detector alive and asks for its status every second, so NexStorm's
  keep-alive only reports whether the Mac is connected. If the connection drops, the connector
  reconnects every few seconds.
- If the Mac can't be reached when NexStorm starts, NexStorm reports that the receiver can't be opened.
  The connector's log (**Show NexStorm Log**, `nexstorm-connector.log` in the bridge's settings folder)
  says why. The token is never logged.
- Settings are read when NexStorm opens the detector: restart NexStorm after changing them.
- **Which settings apply where:** NexStorm's **Receiver squelch** and the alarm values it sends at start-up are
  the detector's own, so they're set on the Mac (NexStorm keeps showing its own squelch number, not the
  detector's). **Noise sensitivity**, **Discard artificial noise**, **Reflection inverter** and **Antenna
  alignment** are NexStorm's own processing and don't affect Lightning Monitor.
- **Squelch scale:** NexStorm shows the LD-250's 0–15 squelch but converts it itself before sending it to an
  LD-350 (its squelch 10 arrives as `SQ 76`; the LD-350's `SQ` is a 0–100 trigger level, lower is more
  sensitive). The connector logs the value NexStorm sends. As a safeguard, a value of 15 or less (only possible
  on the 0–15 scale) is converted with Boltek's table (0 → 40, 1 → 43, 2 → 47, 4 → 53, 8 → 58, 15 → 78) when
  settings are allowed (`nexstorm_squelch_scale` `auto`, `ld250` or `ld350`).
- It works with any LD-350 shared with Lightning Monitor's protocol: Lightning Monitor on a Mac,
  `ld350-proxy`, or this bridge on another computer. An LD-250 works too: keep NexStorm's receiver model on
  LD-350 and the connector converts its lines (`$WIMST` → `$WIMSU`, `:SQUELCH` → `:SQ`, GPS lines dropped).
- **NexStorm's own LD-250 mode:** set `nexstorm_com_port` to the bridge's end of a virtual null-modem pair
  ([com0com](https://sourceforge.net/projects/com0com/), e.g. COM11 ↔ COM12) and set NexStorm to LD-250 on the
  other end. The bridge relays the shared detector there (`lightning-bridge nexstorm relay`, or automatically from
  the tray), filtering NexStorm's commands the same way; an LD-350 is presented as an LD-250 (no waveforms,
  `$WIMSU` → `$WIMST`, squelch converted). Not yet tried with com0com on Windows.
- Not yet tried with NexStorm and a real detector. It has been tested against Lightning Monitor's own
  sharing server (simulated storm) and this bridge's, with Boltek's exports and data layouts.

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
| `nexstorm_host`, `nexstorm_port` | , `10001` | The Mac sharing the detector NexStorm reads ([NexStorm](#nexstorm-reading-a-detector-on-a-mac)) |
| `nexstorm_token` | | The token set in Lightning Monitor's sharing settings |
| `nexstorm_allow_settings` | `false` | Let NexStorm change the detector's settings for every viewer |
| `nexstorm_squelch_scale` | `auto` | How NexStorm's 0–15 squelch becomes the LD-350's 0–100 `SQ`: `auto`, `ld250`, `ld350` |
| `nexstorm_folder` | | NexStorm's folder, if not found automatically |
| `nexstorm_com_port` | | NexStorm's LD-250 mode: the bridge's end of a virtual COM port pair, e.g. `COM11` |

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

`pytest` also builds the NexStorm connector (`nexstorm/ld350_connector.c`) with the computer's C compiler and
runs it against the bridge on loopback (skipped on Windows). The Windows DLL is 32-bit, built by
`packaging/build.py` with 32-bit MSVC or `i686-w64-mingw32-gcc`.

Pushing a `v*` tag builds the Windows and Linux downloads on GitHub Actions and attaches them to the
release. The macOS download is signed and notarised with `packaging/macos/sign-and-notarize.sh`.

## Code signing policy

Windows releases are signed through [SignPath.io](https://signpath.io): free code signing provided by
SignPath.io, certificate by [SignPath Foundation](https://signpath.org).

- Only release tags built from this repository by GitHub Actions are signed, and only this project's own
  programs (`lightning-bridge.exe`, `Lightning Monitor Bridge.exe`, the NexStorm connector
  `nexstorm\LD350_DLL.dll` and `nexstorm\NexStorm Connector Setup.exe`). Bundled third-party files (Python, libusb) are shipped as their authors built them.
- Committers and reviewers: [Tim Brien](https://github.com/brientim). Approver for each signing request:
  [Tim Brien](https://github.com/brientim).
- macOS releases are signed with the maintainer's Apple Developer ID and notarised by Apple.

## Privacy

Lightning Monitor Bridge doesn't collect or send any information to the developer or to the internet. It
only talks to the detector it's connected to and, on your local network, to the Lightning Monitor apps
that connect to it, and it announces itself on the local network with Bonjour (turn this off with
`advertise: false`). On Windows the tray also looks for shared detectors with Bonjour, and the NexStorm
connector connects only to the detector you choose. Its logs stay on your computer.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE). Boltek, LD-350, LD-250, NexStorm and StormVue are trademarks of
their owners; this project is not affiliated with Boltek.
