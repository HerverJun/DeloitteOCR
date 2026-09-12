"""Build-only font fetch. Freeze source commit, OFL and derived static font hashes."""
import hashlib
import json
from pathlib import Path
import urllib.request
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

root = Path(__file__).resolve().parents[1]
output = root / 'build/document-workflow/bundle/fonts'
output.mkdir(parents=True, exist_ok=True)
lockfile = root / 'config/pdf-font-lock.json'
known = json.loads(lockfile.read_text('utf-8')) if lockfile.exists() else {}
def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'OCR-offline-build'}), timeout=120) as response:
        return response.read()
revision = known.get('revision') or json.loads(fetch('https://api.github.com/repos/google/fonts/commits/main'))['sha']
families = [('notosanssc', 'NotoSansSC[wght].ttf', 'NotoSansSC-Regular.ttf'),
            ('notosans', 'NotoSans[wdth,wght].ttf', 'NotoSans-Regular.ttf'),
            ('notosanssymbols2', 'NotoSansSymbols2-Regular.ttf', 'NotoSansSymbols2-Regular.ttf')]
files = []
for family, source, name in families:
    base = f'https://raw.githubusercontent.com/google/fonts/{revision}/ofl/{family}/'
    url = base + urllib.parse.quote(source)
    target = output / name
    if not target.exists():
        content = fetch(url)
        raw = output / ('source-' + name)
        raw.write_bytes(content)
        font = TTFont(raw)
        if 'fvar' in font:
            axes = {axis.axisTag: axis.defaultValue for axis in font['fvar'].axes}
            axes['wght'] = 400
            font = instantiateVariableFont(font, axes, inplace=True)
        font.save(target)
        license = fetch(base + 'OFL.txt')
        (output / (family+'-OFL.txt')).write_bytes(license)
    for path in (target, output / ('source-'+name), output / (family+'-OFL.txt')):
        files.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size,
                      'url': base + ('OFL.txt' if path.suffix == '.txt' else urllib.parse.quote(source))})
    print(name, target.stat().st_size, flush=True)
lockfile.write_text(json.dumps({'repo': 'google/fonts', 'revision': revision, 'license': 'OFL-1.1', 'files': files}, indent=2)+'\n', 'utf-8')
