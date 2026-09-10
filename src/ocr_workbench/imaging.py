"""Immutable originals and geometrically explicit image versions."""

import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance
from pillow_heif import register_heif_opener
from ocr_workbench.store import uid, now, encoded
from ocr_workbench.image_utils import normalized_rgb

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 80_000_000
SUPPORTED = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def add_image(store, project_id, name, temporary):
    store.one("projects", project_id)
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("不支持的图片格式")
    with Image.open(temporary) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise ValueError("多页图片请先拆分为单页")
        if source.width * source.height > 80_000_000:
            raise ValueError("图片超过 8000 万像素，请先缩小")
        image = normalized_rgb(source)
    key, version = uid(), uid()
    folder = store.root / "projects" / project_id / "images" / key
    folder.mkdir(parents=True)
    original = folder / ("original" + suffix)
    temporary.replace(original)
    prepared = folder / (version + ".png")
    image.save(prepared)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?)",
            (
                key,
                project_id,
                name,
                str(original.relative_to(store.root)),
                digest(original),
                version,
                now(),
            ),
        )
        db.execute(
            "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                version,
                key,
                None,
                str(prepared.relative_to(store.root)),
                image.width,
                image.height,
                digest(prepared),
                encoded({"kind": "import", "exif_normalized": True}),
                now(),
            ),
        )
    return store.one("images", key)


def transform(store, version_id, operation):
    import cv2
    import numpy as np

    source = store.one("versions", version_id)
    with Image.open(store.file(source["path"])) as raw:
        image = raw.convert("RGB")
    kind = operation.get("kind")
    if kind == "rotate":
        degrees = operation.get("degrees")
        if degrees not in [90, 180, 270, -90]:
            raise ValueError("旋转角度必须是 90 度的倍数")
        image = image.rotate(-degrees, expand=True)
    elif kind == "crop":
        box = operation.get("box", [])
        if len(box) != 4 or any(type(v) not in {float, int} for v in box):
            raise ValueError("请框选裁剪区域")
        x1, y1, x2, y2 = map(round, box)
        if (
            not (0 <= x1 < x2 <= image.width and 0 <= y1 < y2 <= image.height)
            or min(x2 - x1, y2 - y1) < 8
        ):
            raise ValueError("裁剪区域太小或超出图片")
        image = image.crop((x1, y1, x2, y2))
    elif kind == "perspective":
        points = np.asarray(operation.get("points", []), dtype=np.float32)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise ValueError("请按左上、右上、右下、左下选取四角")
        if (
            (points < 0).any()
            or (points[:, 0] > image.width).any()
            or (points[:, 1] > image.height).any()
        ):
            raise ValueError("透视点超出图片")
        contour = points.reshape(-1, 1, 2)
        if not cv2.isContourConvex(contour) or cv2.contourArea(contour) < 64:
            raise ValueError("四角应构成凸四边形，不能交叉或重合")
        width = round(
            max(
                np.linalg.norm(points[1] - points[0]),
                np.linalg.norm(points[2] - points[3]),
            )
        )
        height = round(
            max(
                np.linalg.norm(points[3] - points[0]),
                np.linalg.norm(points[2] - points[1]),
            )
        )
        if min(width, height) < 8 or width * height > 80_000_000:
            raise ValueError("校正后的尺寸超出限制")
        matrix = cv2.getPerspectiveTransform(
            points,
            np.float32(
                [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
            ),
        )
        image = Image.fromarray(
            cv2.warpPerspective(
                np.asarray(image),
                matrix,
                (width, height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
        )
        operation = {**operation, "matrix": matrix.tolist()}
    elif kind == "contrast":
        factor = operation.get("factor", 1.3)
        if not isinstance(factor, (int, float)) or not 0.2 <= factor <= 3:
            raise ValueError("对比度应在 0.2–3 之间")
        image = ImageEnhance.Contrast(image).enhance(factor)
    else:
        raise ValueError("未知的图像处理操作")
    return save_version(store, source, image, operation)


def save_version(store, parent, image, operation, task_id=None):
    version = uid()
    path = store.file(parent["path"]).with_name(version + ".png")
    image.save(path)
    with store.transaction() as db:
        if task_id:
            task = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task or task["status"] != "running":
                path.unlink()
                return None
        db.execute(
            "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                version,
                parent["image_id"],
                parent["id"],
                str(path.relative_to(store.root)),
                image.width,
                image.height,
                digest(path),
                encoded(operation),
                now(),
            ),
        )
        if task_id:
            db.execute(
                "UPDATE images SET active_version=? WHERE id=? AND active_version=?",
                (version, parent["image_id"], parent["id"]),
            )
            db.execute(
                "UPDATE tasks SET status='succeeded',phase='去弯曲完成',finished=?,result_version_id=? WHERE id=?",
                (now(), version, task_id),
            )
        else:
            db.execute(
                "UPDATE images SET active_version=? WHERE id=?",
                (version, parent["image_id"]),
            )
    return store.one("versions", version)
