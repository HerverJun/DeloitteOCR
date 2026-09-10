"""Invoke pinned upstream conversion in a build runtime (not on the user's PC)."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--bundle', type=Path, required=True)
p.add_argument('--llama-source', type=Path, required=True)
a = p.parse_args()
commit='d344123fe2de081a72e02d6869360dfcbc0b528b'
assets=json.loads((Path(__file__).resolve().parents[1]/'config/build-assets.json').read_text(encoding='utf-8'))
expected=next(asset['converter_sha256'] for asset in assets if 'converter_sha256' in asset)
converter_hash=hashlib.sha256((a.llama_source/'convert_hf_to_gguf.py').read_bytes()).hexdigest()
if converter_hash!=expected:
    raise SystemExit('Converter does not match the audited llama.cpp commit')
source_manifest=json.loads((a.bundle/'models/HunyuanOCR/source-manifest.json').read_text(encoding='utf-8'))
runtime = a.bundle / 'runtimes/glm/python.exe'
output = a.bundle / 'models/HunyuanOCR-GGUF'
output.mkdir(parents=True, exist_ok=True)
for mmproj in [False, True]:
    args = ['--outfile', str(output / ('mmproj-hyocr-f16.gguf' if mmproj else 'hyocr-f16.gguf')),
            '--outtype', 'f16', *(['--mmproj'] if mmproj else []), str(a.bundle/'models/HunyuanOCR')]
    code = 'import sys,runpy; from pathlib import Path; p=Path(sys.argv.pop(1)); sys.path.insert(0,str(p)); sys.path.insert(0,str(p/"gguf-py")); sys.argv[0]=str(p/"convert_hf_to_gguf.py"); runpy.run_path(sys.argv[0],run_name="__main__")'
    subprocess.run([str(runtime), '-X', 'utf8', '-I', '-c', code, str(a.llama_source), *args], check=True)
weights=sorted((a.bundle/'models/HunyuanOCR').glob('*.safetensors'))
if len(weights)!=1:
    raise RuntimeError('Expected the pinned single-file Hunyuan checkpoint')
with weights[0].open('rb') as stream:
    source_hash=hashlib.file_digest(stream,'sha256').hexdigest()
manifest={'repo':source_manifest['repo'],'revision':source_manifest['revision'],
          'conversion':{'llama_commit':commit,'outtype':'f16','source_sha256':source_hash,
                        'converter_sha256':converter_hash}}
(output/'source-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
