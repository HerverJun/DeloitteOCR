from pathlib import Path
import random

from PIL import Image, ImageDraw

from .datasets import publish_dataset, register_asset
from .store import Store, dump, now, uid


def create_demo(settings, per_class=20):
    if not 5 <= per_class <= 200:
        raise ValueError("合成数据每类应为 5 到 200 张")
    store = Store(settings)
    name = "流程验证 · 合成图形"
    prior = store.one("SELECT id FROM projects WHERE name=?", (name,))
    if prior:
        return {"project_id": prior["id"], "existing": True}
    labels = [{"label_id": key, "display_name": label, "definition": f"合成{label}，仅用于软件流程验证"} for key, label in [("circle", "圆形"), ("rectangle", "矩形"), ("triangle", "三角形")]]
    project_id = uid("project")
    with store.connect(write=True) as con:
        con.execute("INSERT INTO projects(id,name,description,labels,created_at) VALUES(?,?,?,?,?)", (project_id, name, "合成图片，仅用于验证导入、训练、导出与分类流程。指标不代表真实业务效果。", dump(labels), now()))
    rng = random.Random(42)
    directory = settings.data_dir / "demo-images"
    for label in labels:
        folder = directory / label["label_id"]
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(per_class):
            image = Image.new("RGB", (rng.randint(280, 420), rng.randint(260, 380)), (246, 246, 243))
            draw = ImageDraw.Draw(image)
            cx, cy = image.width // 2 + rng.randint(-18, 18), image.height // 2 + rng.randint(-18, 18)
            radius = rng.randint(60, 100)
            color = rng.choice([(58, 116, 97), (78, 102, 156), (163, 103, 73), (110, 79, 135)])
            if label["label_id"] == "circle":
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
            elif label["label_id"] == "rectangle":
                draw.rectangle((cx - radius, cy - radius * .75, cx + radius, cy + radius * .75), fill=color)
            else:
                draw.polygon([(cx, cy - radius), (cx - radius, cy + radius), (cx + radius, cy + radius)], fill=color)
            path = folder / f"合成_{label['label_id']}_{index:03}.png"
            image.save(path)
            register_asset(store, project_id, Path(path), label["label_id"], "synthetic-shapes-v1")
    dataset = publish_dataset(store, project_id, "合成流程验证 v1")
    return {"project_id": project_id, "dataset_id": dataset["id"], "image_count": per_class * len(labels), "synthetic": True}
