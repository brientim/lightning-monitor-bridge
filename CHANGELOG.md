# Changelog

## Unreleased

- NexStorm can read an LD-350 plugged into a Mac, at the same time as Lightning Monitor. The Lightning
  Monitor NexStorm Connector (`nexstorm/LD350_DLL.dll`, from `nexstorm/ld350_connector.c`) has the
  functions NexStorm calls in Boltek's `LD350_DLL.dll`, but reads a detector shared with Lightning
  Monitor's protocol: token, `:SERVER` greeting, strike and status lines with CR LF, and waveforms
  unpacked into Boltek's `LD350_CaptureDataType`. It reconnects by itself. NexStorm's setting changes
  are only sent with `nexstorm_allow_settings`; `RAW 0` is never sent. Tray: **NexStorm** menu (pick the
  Mac from those found with Bonjour, Set Up NexStorm with administrator rights when needed, Restore
  Boltek's Driver). Command line: `lightning-bridge nexstorm connect | install | restore | status`.
  The Windows download builds it with 32-bit MSVC and signs it. Not yet tried with NexStorm and a detector.
- **NexStorm Connector Setup.exe** (`nexstorm/connector_setup.c`, also a release asset
  `nexstorm-connector-setup.exe`): carries the connector, finds NexStorm (running copy, uninstall record,
  `C:\Astrogenic\NexStorm`), asks for the computer, port and token, tests the connection with the connector
  itself, closes and restarts NexStorm, installs beside `NexStorm.exe` and in `x86` / `x64` keeping Boltek's
  DLLs, and restores them. `/install /host= /token= …`, `/restore`, `/status`. Built with MSVC (cl, rc) or
  MinGW, signed with the rest.
- Connector: `MessageReady` / `CaptureReady` return a full int, because NexStorm reads all of EAX; a bool
  left the upper bytes undefined, so NexStorm read empty waveforms and counted about 600 noises a minute.
  Found on the Weather PC.
- Connector: blocked and sent commands are logged with their values (NexStorm sends its squelch 10 as
  `SQ 76`, already on the LD-350 scale). As a safeguard, a squelch of 15 or less is converted to the LD-350's
  trigger level with Boltek's table when settings are allowed (`squelch_scale` / `nexstorm_squelch_scale`).
- NexStorm's folder `C:\Astrogenic\NexStorm` (its installer's default) is found; NexStorm loads the
  `LD350_DLL.dll` beside `NexStorm.exe`.
- LD-250: the connector presents a shared LD-250 to NexStorm's LD-350 mode (`$WIMST` → `$WIMSU`,
  `:SQUELCH` → `:SQ`, GPS and EFM-100 lines dropped, NexStorm's squelch converted to 0–15) instead of refusing it.
- NexStorm's own LD-250 mode: `serial_relay.py` relays the shared detector to a virtual COM port pair
  (`nexstorm_com_port`, `lightning-bridge nexstorm relay`, started by the tray when set). An LD-350 is presented
  as an LD-250. Commands are filtered like the connector's. Not yet tried with com0com on Windows.
- With the detector on the PC, NexStorm reads it through this bridge (**This PC's Detector**,
  `nexstorm connect this-pc`), so NexStorm and Lightning Monitor share it there too. The bridge's `boltek`
  driver refuses the connector (it would be reading itself) and `find_boltek_dll` skips it.
- Wording: Boltek's driver lets NexStorm use the detector again when the bridge is closed, not at the
  same time.
- Boltek's driver (`ld350_driver` `boltek`): waveforms and the LTS-3 GPS time now reach Lightning
  Monitor, so strikes are classified. The bridge turns on raw data (`LD350_RawDataOn`) and packs each
  capture from `LD350_GetCaptureData` back into the LD-350's own frame, before the lines that follow
  it. Previously the captures were read and dropped. Not yet tried with a detector.

## 0.1.0 — first release

- Shares a Boltek LD-350 (USB) or LD-250 (serial) with Lightning Monitor, using Lightning Monitor's
  sharing protocol (up to 16 clients, optional token, line limits, slow clients dropped).
- Windows: WinUSB (waveforms) or Boltek's own driver through `LD350_DLL.dll` (NexStorm keeps working);
  `auto` tries WinUSB first. 32-bit build so Boltek's DLL can load.
- Bonjour announcement (`_lightningmon._tcp`), so Lightning Monitor lists it under On this network.
- Tray (Windows, Linux) and menu-bar (macOS) app: status, client count, sharing on / off, Open at Login,
  settings and log.
- Settings file, `devices`, `config`, `token`, and `install-service` for launchd, systemd and a Windows
  scheduled task. Linux `install.sh` with the LD-350 udev rule.
- Grew out of `ld_proxy.py` from the Lightning Monitor repository; its command-line options still work.
