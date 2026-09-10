"""Deterministic synthetic compatibility fixtures; not an accuracy benchmark."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import json

root = Path(__file__).resolve().parents[1] / 'fixtures'
root.mkdir(exist_ok=True)
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 34)
small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 29)
image = Image.new('RGB', (1400, 1000), 'white')
draw = ImageDraw.Draw(image)
lines = ['离线识别兼容性测试', 'Offline OCR Windows Test', '产品编号：00123456789012345678', '日期：2026-09-10  金额：128.50']
for i, text in enumerate(lines):
    draw.text((80, 90 + 95*i), text, fill='black', font=font)
image.save(root / 'printed.png')

image = Image.new('RGB', (1400, 1000), 'white')
draw = ImageDraw.Draw(image)
draw.text((80, 65), '设备验收清单 / Equipment list', fill='black', font=font)
xs, ys = [80, 600, 840, 1080, 1320], [180, 270, 370, 470, 570, 670]
for y in ys:
    draw.line((xs[0], y, xs[-1], y), fill='black', width=3)
for x in xs:
    draw.line((x, ys[1] if x in xs[1:-1] else ys[0], x, ys[-1]), fill='black', width=3)
draw.text((420, 200), '采购记录（合并标题）', fill='black', font=small)
rows = [['编号', '名称', '数量', '备注'], ['00123456789012345678', '扫描仪', '02', '通过'],
        ['00002', '打印机', '01', ''], ['00003', '相机', '03', '复核']]
for r, values in enumerate(rows):
    for c, value in enumerate(values):
        draw.text((xs[c]+15, ys[r+1]+30), value, fill='black', font=small)
image.save(root / 'table.png')
(root / 'expectations.json').write_text(json.dumps({'description': 'Synthetic compatibility only; no accuracy ranking',
    'printed': lines, 'table': {'rows': 5, 'columns': 4, 'header_colspan': 4, 'data': rows}},ensure_ascii=False,indent=2),encoding='utf-8')
