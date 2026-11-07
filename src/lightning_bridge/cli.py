# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line: ``lightning-bridge [command] [options]``.

Commands:
  run                share the detector (the default; also accepts the old ld_proxy.py options)
  tray               the tray / menu-bar app
  devices            list the detectors and serial ports this computer can see
  config             show the settings, or ``config set KEY VALUE`` / ``config path``
  token              show the token, or ``token --new`` to create one
  install-service    start automatically (``--tray`` for the tray app, ``--system`` at boot)
  uninstall-service  stop starting automatically
  service-status     whether it starts automatically
  nexstorm           NexStorm on this PC reads a detector shared by Lightning Monitor on a Mac:
                     ``nexstorm connect HOST``, ``nexstorm install``, ``nexstorm restore``, ``nexstorm status``,
                     ``nexstorm relay --com COM11`` (NexStorm's LD-250 mode through a virtual COM port pair)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from lightning_bridge import APP_NAME, __version__
from lightning_bridge import config as settings

COMMANDS = ("run", "tray", "devices", "config", "token", "install-service", "uninstall-service", "service-status",
            "nexstorm", "version")


def setup_logging(config_path: Path, verbose: bool = False) -> None:
    logger = logging.getLogger("lightning_bridge")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)
    try:
        path = settings.log_path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=1 << 20, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass


def add_run_options(parser: argparse.ArgumentParser) -> None:
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--ld350", action="store_true", help="LD-350 on USB")
    kind.add_argument("--ld250", metavar="SERIAL_PORT", help="LD-250 on a serial port, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument("--driver", choices=settings.LD350_DRIVERS, help="LD-350 driver on Windows (default auto)")
    parser.add_argument("--host", help="address to listen on (default all)")
    parser.add_argument("--port", type=int, help="TCP port (default 10001)")
    parser.add_argument("--token", help="clients must send AUTH <token>")
    parser.add_argument("--name", help="name shown in Lightning Monitor (default this computer's name)")
    parser.add_argument("--no-advertise", action="store_true", help="don't announce on the network (Bonjour)")
    parser.add_argument("--libusb", help="path to libusb-1.0 if it is not found automatically")
    parser.add_argument("--boltek-dll", help="path to Boltek's LD350_DLL.dll")
    parser.add_argument("--verbose", action="store_true")


def apply_run_options(config: settings.Config, options) -> settings.Config:
    if options.ld350:
        config.detector = "ld350"
    if options.ld250:
        config.detector, config.serial_port = "ld250", options.ld250
    for option, key in (("driver", "ld350_driver"), ("host", "host"), ("port", "port"), ("token", "token"),
                        ("name", "name"), ("libusb", "libusb"), ("boltek_dll", "boltek_dll")):
        value = getattr(options, option)
        if value is not None:
            setattr(config, key, value)
    if options.no_advertise:
        config.advertise = False
    config.validate()
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lightning-bridge", description="%s %s: share a Boltek LD-350 or LD-250 with Lightning Monitor." % (APP_NAME, __version__))
    parser.add_argument("--config", type=Path, help="settings file (default %s)" % settings.default_config_path())
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("run", help="share the detector")
    add_run_options(run)
    tray = commands.add_parser("tray", help="tray / menu-bar app")
    tray.add_argument("--verbose", action="store_true")
    commands.add_parser("devices", help="list detectors and serial ports")
    config_command = commands.add_parser("config", help="show or change settings")
    config_command.add_argument("action", nargs="?", choices=("show", "set", "path"), default="show")
    config_command.add_argument("key", nargs="?")
    config_command.add_argument("value", nargs="?")
    token = commands.add_parser("token", help="show or create the token")
    token.add_argument("--new", action="store_true", help="create a new token and save it")
    token.add_argument("--clear", action="store_true", help="remove the token (anyone can connect)")
    install = commands.add_parser("install-service", help="start automatically")
    install.add_argument("--tray", action="store_true", help="start the tray app at login instead of the headless service")
    install.add_argument("--system", action="store_true", help="start at boot for all users (needs admin / sudo)")
    uninstall = commands.add_parser("uninstall-service", help="stop starting automatically")
    uninstall.add_argument("--system", action="store_true")
    commands.add_parser("service-status", help="whether it starts automatically")
    nexstorm = commands.add_parser("nexstorm", help="NexStorm on this PC reads a detector shared by Lightning Monitor")
    nexstorm.add_argument("action", nargs="?", choices=("status", "connect", "install", "restore", "relay"), default="status")
    nexstorm.add_argument("host", nargs="?",
                          help="connect: the Mac sharing the detector, e.g. My-Mac.local, or this-pc for this bridge's own detector")
    nexstorm.add_argument("--port", type=int, help="the Mac's sharing port (default 10001)")
    nexstorm.add_argument("--token", help="the token set in Lightning Monitor's sharing settings")
    allow = nexstorm.add_mutually_exclusive_group()
    allow.add_argument("--allow-settings", dest="allow_settings", action="store_true", default=None,
                       help="let NexStorm change the detector's settings for every viewer")
    allow.add_argument("--no-allow-settings", dest="allow_settings", action="store_false")
    nexstorm.add_argument("--folder", help="NexStorm's folder, if not found automatically")
    nexstorm.add_argument("--com", help="relay: the bridge's end of a virtual COM port pair, e.g. COM11 (NexStorm on the other end, in LD-250 mode)")
    commands.add_parser("version", help="show the version")
    for sub in commands.choices.values():
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def normalise(argv: list[str]) -> list[str]:
    """``lightning-bridge --ld250 COM3`` (the old ld_proxy.py style) means ``run --ld250 COM3``."""
    if not argv:
        return ["tray"] if getattr(sys, "frozen", False) and _windowed() else ["run"]
    if argv[0] in ("-h", "--help", "--version"):
        return argv
    if argv[0] == "--config" and len(argv) >= 2:
        return argv[:2] + normalise(argv[2:])
    if argv[0] not in COMMANDS:
        return ["run"] + argv
    return argv


def _windowed() -> bool:
    """A frozen GUI build (macOS .app, Windows windowed exe) has no console."""
    return sys.stdout is None or sys.platform == "darwin" and ".app/Contents/MacOS" in sys.executable


def run_bridge(config: settings.Config) -> None:
    from lightning_bridge.server import Bridge

    bridge = Bridge(config)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                try:
                    loop.add_signal_handler(getattr(signal, name), bridge.stop)
                except NotImplementedError:  # Windows
                    pass
        await bridge.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def run_nexstorm(config_path: Path, options) -> None:
    from lightning_bridge import nexstorm

    config = settings.load(config_path)
    changed = False
    if options.host == "this-pc":
        # The detector on this PC's own USB, shared by this bridge: NexStorm reads it beside the other viewers.
        options.host = "127.0.0.1"
        options.port = options.port or config.port
        options.token = options.token if options.token is not None else config.effective_token or ""
    for value, key in ((options.folder, "nexstorm_folder"), (options.host, "nexstorm_host"), (options.port, "nexstorm_port"),
                       (options.token, "nexstorm_token"), (options.allow_settings, "nexstorm_allow_settings"),
                       (options.com, "nexstorm_com_port")):
        if value is not None and getattr(config, key) != value:
            setattr(config, key, value)
            changed = True
    if options.host and options.action != "connect":
        sys.exit("usage: lightning-bridge nexstorm connect HOST [--port PORT] [--token TOKEN]")
    try:
        config.validate()
    except ValueError as error:
        sys.exit(str(error))
    if changed or options.action == "connect":
        settings.save(config, config_path)
        if config.nexstorm_host:
            nexstorm.write_settings(config)

    if options.action == "relay":
        if not config.nexstorm_host or not config.nexstorm_com_port:
            sys.exit("usage: lightning-bridge nexstorm relay --com COM11 (after: lightning-bridge nexstorm connect HOST)")
        from lightning_bridge.serial_relay import SerialRelay

        setup_logging(config_path, False)
        relay = SerialRelay(config)
        print("Relaying %s:%d to %s for NexStorm's LD-250 mode (Ctrl+C stops)" % (config.nexstorm_host, config.nexstorm_port, config.nexstorm_com_port))
        try:
            relay.run()
        except KeyboardInterrupt:
            relay.stop()
        return

    try:
        if options.action == "connect":
            if not config.nexstorm_host:
                sys.exit("usage: lightning-bridge nexstorm connect HOST [--port PORT] [--token TOKEN]")
            print("NexStorm will read the detector shared at %s:%d. Restart NexStorm if it is running."
                  % (config.nexstorm_host, config.nexstorm_port))
            folder = nexstorm.find_nexstorm(config.nexstorm_folder)
            if folder is None or nexstorm.state(folder) != "installed":
                print("Then put the connector in NexStorm's folder: lightning-bridge nexstorm install")
        elif options.action in ("install", "restore"):
            folder = nexstorm.find_nexstorm(config.nexstorm_folder)
            if folder is None:
                sys.exit("NexStorm not found; add --folder with NexStorm's folder")
            if options.action == "install":
                if not config.nexstorm_host:
                    print("Note: no detector set yet; run: lightning-bridge nexstorm connect HOST")
                print("\n".join(nexstorm.install(folder)))
                print("Quit NexStorm (if it's running) and start it again.")
            else:
                print("\n".join(nexstorm.restore(folder)))
        else:
            print("\n".join(nexstorm.describe(config)))
    except nexstorm.NexStormError as error:
        sys.exit(str(error))


def main(argv: list[str] | None = None) -> None:
    argv = normalise(list(sys.argv[1:] if argv is None else argv))
    if argv == ["--version"]:
        print("%s %s" % (APP_NAME, __version__))
        return
    options = build_parser().parse_args(argv)
    config_path = getattr(options, "config", None) or settings.default_config_path()
    command = options.command or "run"

    if command == "version":
        print("%s %s" % (APP_NAME, __version__))
        return
    if command == "devices":
        from lightning_bridge.devices import describe_detectors

        config = settings.load(config_path)
        print("\n".join(describe_detectors(config.libusb or None)))
        return
    if command == "config":
        config = settings.load(config_path)
        if options.action == "path":
            print(config_path)
        elif options.action == "set":
            if not options.key or options.value is None:
                sys.exit("usage: lightning-bridge config set KEY VALUE")
            try:
                config.set(options.key, options.value)
            except (KeyError, ValueError) as error:
                sys.exit(str(error))
            print("Saved %s" % settings.save(config, config_path))
        else:
            shown = asdict(config)
            if shown["token"]:
                shown["token"] = "(set)"
            print(json.dumps(shown, indent=2))
        return
    if command == "token":
        config = settings.load(config_path)
        if options.new or options.clear:
            config.token = "" if options.clear else settings.new_token()
            settings.save(config, config_path)
        print(config.token or "No token: anyone on the network can connect. Create one with: lightning-bridge token --new")
        return
    if command in ("install-service", "uninstall-service", "service-status"):
        from lightning_bridge import service

        try:
            if command == "install-service":
                if not config_path.exists():
                    settings.save(settings.load(config_path), config_path)
                print(service.install("tray" if options.tray else "run", options.system, config_path))
            elif command == "uninstall-service":
                print(service.uninstall(options.system))
            else:
                print(service.status())
        except (PermissionError, OSError) as error:
            sys.exit(str(error))
        return
    if command == "nexstorm":
        run_nexstorm(config_path, options)
        return

    setup_logging(config_path, getattr(options, "verbose", False))
    if command == "tray":
        from lightning_bridge.tray import run_tray

        run_tray(config_path)
        return
    try:
        config = apply_run_options(settings.load(config_path), options)
    except ValueError as error:
        sys.exit(str(error))
    run_bridge(config)
