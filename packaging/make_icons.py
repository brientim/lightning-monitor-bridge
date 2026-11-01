# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes build/icons/bridge.ico (Windows) and build/icons/bridge.icns (macOS) from the drawn bolt."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lightning_bridge.icon import app_icon  # noqa: E402

output = Path(__file__).resolve().parents[1] / "build" / "icons"
output.mkdir(parents=True, exist_ok=True)
image = app_icon(1024)
image.save(output / "bridge.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
if sys.platform == "darwin":
    image.save(output / "bridge.icns")
image.resize((256, 256)).save(output / "bridge.png")
print("Icons written to", output)
