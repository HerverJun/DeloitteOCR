from pathlib import Path
import argparse
import json
from fetch_assets import snapshot

REPOS = [
    'PaddlePaddle/PP-OCRv6_medium_det',
    'PaddlePaddle/PP-OCRv6_medium_rec',
    'PaddlePaddle/PP-LCNet_x1_0_textline_ori',
    'PaddlePaddle/PP-LCNet_x1_0_doc_ori',
    'PaddlePaddle/UVDoc',
    'PaddlePaddle/PP-DocLayoutV3',
    'PaddlePaddle/PaddleOCR-VL-1.6',
    'PaddlePaddle/PP-DocLayoutV3_safetensors',
    'zai-org/GLM-OCR',
    'tencent/HunyuanOCR',
]
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--proxy')
    parser.add_argument('--output',type=Path,default=Path('build/models'))
    args = parser.parse_args()
    lock_path=Path(__file__).resolve().parents[1]/'config/model-lock.json'
    locked=json.loads(lock_path.read_text(encoding='utf-8')) if lock_path.is_file() else {}
    failures = []
    for repo in REPOS:
        try:
            snapshot(repo, args.output, 'https://hf-mirror.com', args.proxy,
                     locked.get(repo.split('/')[-1],{}).get('revision'))
        except Exception as error:
            failures.append(repo)
            print(f'FAILED {repo}: {error}', flush=True)
    if failures:
        raise SystemExit(f'Failed models: {failures}')
