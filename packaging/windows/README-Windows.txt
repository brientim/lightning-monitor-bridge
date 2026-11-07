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
  boltek  Boltek's own driver: NexStorm can use the detector again when the bridge is closed (one
          program at a time). Waveforms come through too, so Lightning Monitor can classify strikes
          (not yet tried with a detector).
          Download Boltek's LD-350 Programmers Toolkit (https://downloads.boltek.com/LD350_DLL.zip)
          and put LD350_DLL.dll in this folder. It needs the Visual C++ 2013 (x86) runtime.

Quit NexStorm before starting the bridge: only one program can open the detector.

NexStorm reading a detector on a Mac
------------------------------------
The other way round: the LD-350 stays on the Mac's USB and Lightning Monitor shares it (Settings >
Detector > Sharing). NexStorm on this PC reads it from the network, at the same time as Lightning
Monitor. The "nexstorm" folder holds the Lightning Monitor NexStorm Connector, a stand-in for Boltek's
LD350_DLL.dll that reads the Mac's detector instead of USB.

Simplest: run nexstorm\NexStorm Connector Setup.exe and choose 1. It asks for the Mac's name, port and
token, tests the connection, closes NexStorm, installs the connector and starts NexStorm again. 3 restores
Boltek's driver. Or with the tray:

  1. Quit NexStorm.
  2. Right-click the bolt > NexStorm > Read the Detector From > the Mac. If it isn't listed, or the Mac
     uses a token, set nexstorm_host and nexstorm_token in Settings..., then Reload Settings.
  3. NexStorm > Set Up NexStorm (Windows asks for administrator rights if NexStorm is in Program Files).
     Boltek's DLLs are kept as LD350_DLL.dll.boltek.
  4. Start NexStorm.

Detector on this PC instead: the bridge opens it and shares it, and NexStorm reads it through the bridge
at the same time as Lightning Monitor. Choose NexStorm > Read the Detector From > This PC's Detector
(or "lightning-bridge nexstorm connect this-pc") and keep Sharing on. With the boltek driver, Boltek's own
LD350_DLL.dll must be next to the bridge (not the connector).

An LD-250: keep NexStorm's receiver model on LD-350; the connector converts the LD-250's lines. For NexStorm's
own LD-250 mode instead, create a com0com virtual port pair (e.g. COM11 and COM12), set nexstorm_com_port to
COM11 in Settings..., Reload Settings, and set NexStorm to LD-250 on COM12. The NexStorm menu shows the relay.

NexStorm > Restore Boltek's Driver puts NexStorm back on this PC's USB. NexStorm's own setting changes
(squelch, alarms, SAVE) change the detector for everyone, so they're only sent with "Let NexStorm Change
Detector Settings" on. Show NexStorm Log says why NexStorm can't open the "receiver".

NexStorm shows the LD-250's 0-15 squelch but sends the LD-350's 0-100 trigger level (its squelch 10 is
SQ 76). Set the squelch on the Mac: NexStorm keeps showing its own number. NexStorm's Noise
sensitivity, Discard artificial noise, Reflection inverter and Antenna alignment only affect NexStorm.

LD-250
------
Settings…: set "detector" to "ld250" and "serial_port" to the COM port (e.g. COM3), then Reload Settings.

Command line
------------
  lightning-bridge devices                 what this PC can see
  lightning-bridge token --new             require a token (enter it in Lightning Monitor)
  lightning-bridge run                     share without the tray icon
  lightning-bridge install-service         start at logon (add --system to start at boot, as admin)
  lightning-bridge nexstorm connect HOST [--token TOKEN] [--allow-settings]   (HOST "this-pc": this PC's detector)
  lightning-bridge nexstorm install        put the connector in NexStorm's folder (as admin)
  lightning-bridge nexstorm restore        put Boltek's DLLs back
  lightning-bridge nexstorm status
  lightning-bridge nexstorm relay --com COM11   NexStorm's LD-250 mode through a virtual COM port pair
