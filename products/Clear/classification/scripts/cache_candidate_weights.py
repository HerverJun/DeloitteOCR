"""Cache pinned public safetensors via HTTPS, with mandatory repository SHA256 checks."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from download_wheel import download

from image_platform.images import file_sha
from image_platform.settings import Settings
from image_platform.training import ENCODERS


def acquire(key):
    info = ENCODERS[key]
    repo = "timm/" + info["name"]
    destination = Settings.from_env().data_dir / "cache/huggingface/hub" / ("models--" + repo.replace("/", "--")) / "snapshots" / info["revision"] / "model.safetensors"
    destination.parent.mkdir(parents=True, exist_ok=True)
    canonical = f"https://huggingface.co/{repo}/resolve/{info['revision']}/model.safetensors"
    url = canonical.replace("huggingface.co", "hf-mirror.com")
    if not destination.exists() or file_sha(destination) != info["sha256"]:
        download(url, destination, {"mobilenetv4": 15223016, "repvit": 22232904}[key], info["sha256"])
    print(key, "pinned SHA256 verified", flush=True)
    return {"encoder": key, "canonical_url": canonical, "download_url": url, "revision": info["revision"], "sha256": info["sha256"], "path": str(destination)}


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(acquire, ["mobilenetv4", "repvit"]))
    Path("sources/lightweight-weights.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
