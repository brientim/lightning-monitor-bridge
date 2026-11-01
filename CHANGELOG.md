# Changelog

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
