"""Build an offline UI glyph subset from the project's OFL-licensed Noto Sans SC."""
from pathlib import Path
import argparse
import shutil
from fontTools import subset

parser = argparse.ArgumentParser()
parser.add_argument('--font', type=Path, required=True)
parser.add_argument('--portal', type=Path, required=True)
args = parser.parse_args()
output = args.portal / 'public' / 'brand' / 'fonts'
output.mkdir(parents=True, exist_ok=True)
characters = set(chr(code) for code in range(32, 127))
for source in (args.portal / 'src').rglob('*'):
    if source.suffix in {'.ts', '.tsx', '.css'}:
        characters.update(source.read_text(encoding='utf-8'))
options = subset.Options()
options.flavor = 'woff2'
font = subset.load_font(str(args.font), options)
subsetter = subset.Subsetter(options=options)
subsetter.populate(text=''.join(sorted(characters)))
subsetter.subset(font)
subset.save_font(font, str(output / 'clear-sans-sc.woff2'), options)
shutil.copyfile(args.font.with_name('OFL.txt'), output / 'OFL.txt')
print(f'{len(characters)} characters; {(output / "clear-sans-sc.woff2").stat().st_size} bytes')
