"""Assemble inference-only source, a verified model, schema and pinned CPU dependencies."""
import argparse
import json
from pathlib import Path
import shutil
import zipfile

from image_platform.images import file_sha
from image_platform.runtime import validate_package
from image_platform.settings import Settings
from image_platform.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--encoder', choices=['mobilenetv4', 'repvit'], default='mobilenetv4')
    args = parser.parse_args()
    report = json.loads(Path('reports/flowers102-benchmark.json').read_text(encoding='utf-8'))
    model_id = report['recommendations'][args.encoder]
    row = Store(Settings.from_env()).one('SELECT package_path FROM models WHERE id=?', (model_id,))
    metadata = validate_package(row['package_path'])
    root = Path('dist') / f'inference-cpu-{args.encoder}'
    source = root / 'src/image_platform'
    source.mkdir(parents=True, exist_ok=True)
    (source / '__init__.py').write_text('', encoding='utf-8')
    for name in ['images.py', 'runtime.py', 'settings.py', 'predict.py']:
        shutil.copyfile(Path('src/image_platform') / name, source / name)
    (root / 'schemas').mkdir(exist_ok=True)
    shutil.copyfile('schemas/model-metadata.schema.json', root / 'schemas/model-metadata.schema.json')
    shutil.copytree(row['package_path'], root / 'model', dirs_exist_ok=True)
    shutil.copyfile('requirements-cpu-inference.lock.txt', root / 'requirements.txt')
    (root / 'predict.py').write_text('''import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from image_platform.predict import predict_images

parser = argparse.ArgumentParser(description="CPU image classification; no PyTorch dependency")
parser.add_argument("images", nargs="+")
parser.add_argument("--output")
args = parser.parse_args()
result = predict_images(ROOT / "model", args.images, device="cpu", auto_accept=False)
rendered = json.dumps(result, ensure_ascii=False, indent=2)
if args.output:
    Path(args.output).write_text(rendered + "\\n", encoding="utf-8")
print(rendered)
''', encoding='utf-8')
    (root / 'README.md').write_text(f'''# CPU 图像分类推理包

模型：{args.encoder}，{model_id}；102 类 Flowers102，224 px，FP32。
依赖 Python 3.12 x64。无需安装 PyTorch、timm、CUDA、训练服务或网页服务。
本包包含源码和模型；Python 解释器与依赖需另行安装，不是单文件可执行程序。

```powershell
python -m venv .venv
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe predict.py C:\\images\\flower.jpg --output result.json
```

默认结果全部待复核。模型仅在去重的 Flowers102 固定划分上评测，不能证明业务准确率。
模型选择只依据验证集。权重来源、许可证、校验和及训练配置见 model/。
ONNX SHA256：{metadata['provenance']['model_onnx_sha256']}。
模型包中的 head.pt 为训练归档；推理不会加载或执行它。
''', encoding='utf-8')
    validate_package(root / 'model')
    archive = root.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as target:
        for path in sorted(root.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                target.write(path, path.relative_to(root))
    print(json.dumps({'bundle': str(archive.resolve()), 'sha256': file_sha(archive), 'bytes': archive.stat().st_size, 'model_id': model_id}))


if __name__ == '__main__':
    main()
