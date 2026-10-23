"""Render the code-defined application mark to a Windows multi-size icon."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
picture = Image.new('RGBA', (128, 128), (0, 0, 0, 0))
draw = ImageDraw.Draw(picture)
draw.rounded_rectangle((3, 3, 124, 124), radius=28, fill='#6dd3cf')
draw.line([(41, 42), (80, 42), (85, 47), (85, 58), (42, 89), (87, 89)], fill='#102638', width=12, joint='curve')
(root / 'build').mkdir(exist_ok=True)
picture.save(root / 'build' / 'twonote.ico', sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128)])
