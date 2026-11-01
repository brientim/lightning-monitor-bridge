Lightning Monitor Bridge for Windows
====================================

Shares a Boltek LD-350 (USB) or LD-250 (serial) with Lightning Monitor on your Mac, iPhone or iPad.

Start
-----
Double-click "Lightning Monitor Bridge.exe". A bolt appears in the notification area (it is yellow when
the detector is connected). Right-click it for status, settings, "Open at Login" and Quit.
Allow it through Windows Defender Firewall on private networks when Windows asks.

Lightning Monitor finds it under Settings > Detector > On this network.

LD-350: choose a driver
-----------------------
The bridge can read the LD-350 two ways ("ld350_driver" in Settings…):

  auto    (default) WinUSB if installed, otherwise Boltek's driver.
  libusb  WinUSB: full data including waveforms, so Lightning Monitor can classify strikes.
          Install WinUSB for the LD-350 with Zadig (https://zadig.akeo.ie): Options > List All
          Devices, pick "LD-350 Lightning Detector (Interface 0)" (USB ID 0403 F241 00), choose
          WinUSB, Replace Driver.
          NexStorm and Boltek's software can't use the detector while WinUSB is installed.
  boltek  Boltek's own driver: NexStorm keeps working (one program at a time), but no waveforms.
          Download Boltek's LD-350 Programmers Toolkit (https://downloads.boltek.com/LD350_DLL.zip)
          and put LD350_DLL.dll in this folder. It needs the Visual C++ 2013 (x86) runtime.

Quit NexStorm before starting the bridge: only one program can open the detector.

LD-250
------
Settings…: set "detector" to "ld250" and "serial_port" to the COM port (e.g. COM3), then Reload Settings.

Command line
------------
  lightning-bridge devices                 what this PC can see
  lightning-bridge token --new             require a token (enter it in Lightning Monitor)
  lightning-bridge run                     share without the tray icon
  lightning-bridge install-service         start at logon (add --system to start at boot, as admin)
