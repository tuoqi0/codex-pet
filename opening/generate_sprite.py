#!/usr/bin/env python3
"""从角色原图和翼层生成两个开场共用的 2× 透明动作图集。"""
import sys
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from codex_pet import _GESTURE_REGIONS, blink_overlay, gesture_overlay, wave_alternate
import dragon_motion

FRAME_SIZE = (924, 522)
ART_SIZE = (840, 474)
MARGINS = (42, 24)


def make_body(art, cutout):
    """用高分辨率原图绘制身体，使用已有翼层遮罩保留正确的前后关系。"""
    art = art.copy()
    art.thumbnail(ART_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', FRAME_SIZE)
    canvas.paste(art, (MARGINS[0] + (ART_SIZE[0] - art.width) // 2,
                       MARGINS[1] + (ART_SIZE[1] - art.height) // 2))
    canvas.putalpha(ImageChops.multiply(canvas.getchannel('A'), ImageChops.invert(cutout)))
    return canvas


def generate():
    assets = ROOT / 'assets'
    base = assets / 'dragon-seated.png'
    with Image.open(base) as source:
        idle = source.convert('RGBA')
    blink = blink_overlay(base, assets / 'dragon-seated-blink.png')
    wave = gesture_overlay(base, assets / 'dragon-seated-wave.png', _GESTURE_REGIONS['wave'])
    alternate = wave_alternate(assets / 'dragon-seated-wave.png', assets / 'dragon-seated-wave2.png')
    wave2 = gesture_overlay(base, alternate, _GESTURE_REGIONS['wave'])
    cutout = dragon_motion._wing_assets()[0].resize(FRAME_SIZE, Image.Resampling.LANCZOS)
    bodies = [make_body(pose, cutout) for pose in (idle, blink, wave, wave2)]
    sheet = Image.new('RGBA', (FRAME_SIZE[0] * 8, FRAME_SIZE[1] * 4))
    backgrounds = {}
    for index in range(32):
        phase = index % 24
        if phase not in backgrounds:
            backgrounds[phase] = dragon_motion._wing_background(phase).resize(
                FRAME_SIZE, Image.Resampling.LANCZOS)
        pose = 1 if index == 6 else 2 if 10 <= index < 17 or 22 <= index < 29 else 3 if 17 <= index < 22 else 0
        frame = backgrounds[phase].copy()
        frame.alpha_composite(bodies[pose])
        if index in (5, 7):
            closed = backgrounds[phase].copy()
            closed.alpha_composite(bodies[1])
            frame = Image.blend(frame, closed, .5)
        sheet.paste(frame, ((index % 8) * FRAME_SIZE[0], (index // 8) * FRAME_SIZE[1]))
    sheet.save(assets / 'opening-sprite.png', optimize=True)


if __name__ == '__main__':
    generate()
