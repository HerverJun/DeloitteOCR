"""Create a reproducible teaching subset from the verified official Flowers102 cache."""
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
from PIL import Image
from scipy.fft import dctn
from scipy.io import loadmat

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('E:/image-platform-data/public/flowers102')
DEST = Path('E:/image-platform-data/tutorial-flowers')
CLASSES = [(49, 'daisy', '牛眼菊'), (74, 'rose', '玫瑰'), (54, 'sunflower', '向日葵')]


def phash(path):
    with Image.open(path) as image:
        pixels = np.asarray(image.convert('L').resize((32, 32)), dtype=float)
    values = dctn(pixels, norm='ortho')[:8, :8].ravel()[1:]
    return values > np.median(values)


def main():
    labels = loadmat(SOURCE / 'imagelabels.mat')['labels'].ravel()
    splits = loadmat(SOURCE / 'setid.mat')
    records, fingerprints, hashes = [], [], set()
    for category, label_id, display_name in CLASSES:
        for role, keys, count in [('prepare', ['trnid', 'valid'], 20), ('classify', ['tstid'], 10)]:
            ids = sorted(int(i) for key in keys for i in splits[key].ravel() if labels[i - 1] == category)
            chosen = 0
            for image_id in ids:
                original = SOURCE / 'jpg' / f'image_{image_id:05d}.jpg'
                digest = hashlib.sha256(original.read_bytes()).hexdigest()
                fingerprint = phash(original)
                if digest in hashes or any(np.count_nonzero(fingerprint != previous) <= 6 for previous in fingerprints):
                    continue
                manual = role == 'prepare' and label_id == 'rose' and chosen == 0
                folder = DEST / ('01-prepare' if role == 'prepare' else '02-classify')
                if role == 'prepare':
                    folder /= 'unlabeled' if manual else label_id
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / original.name
                shutil.copy2(original, target)
                official_split = next(key for key in keys if image_id in splits[key])
                records.append(dict(image_id=image_id, filename=original.name, label_id=label_id,
                    display_name=display_name, official_category=category, official_split=official_split,
                    role=role, manual_annotation=manual, path=str(target), sha256=digest))
                fingerprints.append(fingerprint)
                hashes.add(digest)
                chosen += 1
                if chosen == count:
                    break
            assert chosen == count, (category, role, chosen)
    distance = min(int(np.count_nonzero(a != b)) for i, a in enumerate(fingerprints) for b in fingerprints[i+1:])
    manifest = dict(dataset='Oxford Flowers102 teaching subset',
        homepage='https://www.robots.ox.ac.uk/~vgg/data/flowers/102/',
        policy='Official train/val supply 60 preparation images; official test supplies 30 unseen images. No selection by model performance.',
        split_seed=42, minimum_phash_distance=distance, exact_duplicates=0,
        classes=[dict(label_id=i, display_name=n, official_category=c) for c, i, n in CLASSES], records=records)
    (DEST / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (DEST / 'README.txt').write_text('公开来源：'+manifest['homepage']+'\n\n01-prepare：60 张准备样本；子目录即类别，其中 unlabeled 内 1 张玫瑰用于演示人工标注。\n02-classify：30 张独立待分类照片；不导入真实标签。\nmanifest.json：来源、官方划分、真实类别与 SHA256，用于核验。\n仅供本地教学和评估；图片权利归原作者，其他用途请核对原始数据使用条件。\n', encoding='utf-8')
    print(json.dumps(dict(destination=str(DEST), counts={'prepare':60,'classify':30}, minimum_phash_distance=distance), ensure_ascii=False))


if __name__ == '__main__':
    main()
