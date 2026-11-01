# SPDX-License-Identifier: GPL-3.0-or-later
"""The bolt icon, drawn with Pillow so no image files are needed."""

from __future__ import annotations

# A lightning bolt on a 0-100 grid.
BOLT = [(58, 4), (18, 56), (44, 56), (36, 96), (82, 38), (55, 38), (66, 4)]
ACTIVE = (255, 196, 0, 255)
INACTIVE = (150, 150, 150, 255)


def bolt_image(size: int = 64, active: bool = True, background=None):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if background is not None:
        radius = size // 5
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=background)
    scale = size / 100.0
    draw.polygon([(x * scale, y * scale) for x, y in BOLT], fill=ACTIVE if active else INACTIVE)
    return image


def app_icon(size: int = 1024):
    """The application icon: the bolt on a dark rounded square."""
    return bolt_image(size, True, background=(24, 30, 46, 255))
