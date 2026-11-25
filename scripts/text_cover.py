"""Render a square, local text cover using the package's display names."""

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from qa_phone import FONT_PATHS, wrapped_lines


NAME_KEYS = ("SSplayerProjectName", "SSplayerRoutineName")
PALETTES = (
    ("#EAF1FF", "#1B3260", "#3864CF"),
    ("#EDF5F0", "#1C4339", "#288270"),
    ("#FFF1E5", "#54312A", "#B05C42"),
    ("#F1EDFA", "#3C305B", "#7553B1"),
)


def fit_title(draw, value, face, start, max_height):
    for size in range(start, 23, -2):
        font = ImageFont.truetype(face, size) if face else ImageFont.load_default(size=size)
        title = "\n".join(wrapped_lines(draw, value, font, 824, len(value)))
        spacing = round(size * 0.15)
        bounds = draw.multiline_textbbox((0, 0), title, font=font, spacing=spacing, align="center")
        if bounds[3] - bounds[1] <= max_height:
            return title, font, spacing, bounds
    raise ValueError("封面文字放不下，请为显示名称选择更简洁的写法。")


def render(destination, *, project, routine, program, font_path=None):
    if destination.suffix.lower() != ".png":
        raise ValueError("文字封面请输出为 PNG。")
    face = str(font_path) if font_path else next((path for path in FONT_PATHS if Path(path).is_file()), None)
    font = ImageFont.truetype(face, 32) if face else ImageFont.load_default(size=32)
    missing = bytes(font.getmask("\u0378"))
    if any(bytes(font.getmask(char)) == missing for char in set(project + routine) if not char.isspace()):
        raise ValueError("当前字体无法完整显示名称。请用 --font 指定合适的字体，例如 Noto Sans CJK。")

    fill, ink, accent = PALETTES[hashlib.sha256(program.encode()).digest()[0] % len(PALETTES)]
    image = Image.new("RGB", (1024, 1024), fill)
    draw = ImageDraw.Draw(image)
    project_layout = fit_title(draw, project, face, 146, 250)
    routine_layout = fit_title(draw, routine, face, 210, 456)
    space_between = 56
    total_height = sum(layout[3][3] - layout[3][1] for layout in (project_layout, routine_layout))
    top = (image.height - total_height - space_between) // 2
    for (title, font, spacing, (left, upper, right, lower)), color in (
        (project_layout, accent), (routine_layout, ink),
    ):
        x = (image.width - (right - left)) // 2 - left
        draw.multiline_text((x, top - upper), title, font=font, spacing=spacing, fill=color, align="center")
        top += lower - upper + space_between
    metadata = PngImagePlugin.PngInfo()
    for key, value in zip(NAME_KEYS, (project, routine)):
        metadata.add_itxt(key, value)
    with destination.open("xb") as output:
        image.save(output, "PNG", optimize=True, pnginfo=metadata)
