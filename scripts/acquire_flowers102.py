"""Acquire the official Oxford Flowers102 archives with pinned torchvision checksums."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tarfile
import time
from urllib.request import Request, urlopen


ROOT = Path("E:/image-platform-data/public/flowers102")
BASE = "https://www.robots.ox.ac.uk/~vgg/data/flowers/102/"
FILES = {"102flowers.tgz": (344862509, "52808999861908f626f3c1f4e79d11fa"),
         "imagelabels.mat": (None, "e0620be6f572b9609742df49c70aed4d"),
         "setid.mat": (None, "a5357ecc9cb78c4bef273ce3793fc85c")}


def digest(path, algorithm):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def acquire(name, size, expected):
    path = ROOT / name
    if path.exists() and digest(path, "md5") == expected:
        return path
    temp = path.with_suffix(path.suffix + ".download")
    if size:
        parts = ROOT / ".parts"
        parts.mkdir(exist_ok=True)
        chunk = 4 * 1024 * 1024
        bounds = [(start, min(start + chunk, size) - 1) for start in range(0, size, chunk)]

        def fetch(bound):
            start, end = bound
            piece = parts / f"{start:012d}.part"
            if piece.exists() and piece.stat().st_size == end - start + 1:
                return piece
            for attempt in range(4):
                try:
                    request = Request(BASE + name + f"?range={start}", headers={"Range": f"bytes={start}-{end}", "User-Agent": "ImagePlatform/0.2"})
                    with urlopen(request, timeout=45) as response:
                        if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                            raise ValueError("Archive range mismatch")
                        data = response.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise ValueError("Incomplete range")
                    piece.write_bytes(data)
                    return piece
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(attempt + 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, future in enumerate(as_completed([pool.submit(fetch, b) for b in bounds]), 1):
                future.result()
                if i % 8 == 0 or i == len(bounds):
                    print(f"Oxford archive: {i}/{len(bounds)} chunks", flush=True)
        with temp.open("wb") as stream:
            for start, _ in bounds:
                stream.write((parts / f"{start:012d}.part").read_bytes())
    else:
        with urlopen(Request(BASE + name, headers={"User-Agent": "ImagePlatform/0.2"}), timeout=30) as response:
            temp.write_bytes(response.read())
    if digest(temp, "md5") != expected:
        raise ValueError(f"Official torchvision checksum mismatch: {name}")
    temp.replace(path)
    return path


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    provenance = []
    for name, (size, expected) in FILES.items():
        path = acquire(name, size, expected)
        provenance.append({"url": BASE + name, "path": str(path), "bytes": path.stat().st_size,
                           "expected_md5_from_torchvision_0_24_1": expected, "sha256": digest(path, "sha256")})
    if len(list((ROOT / "jpg").glob("image_*.jpg"))) != 8189:
        with tarfile.open(ROOT / "102flowers.tgz") as archive:
            for member in archive.getmembers():
                destination = (ROOT / member.name).resolve()
                if not destination.is_relative_to(ROOT.resolve()) or member.issym() or member.islnk():
                    raise ValueError("Unsafe archive entry")
            archive.extractall(ROOT, filter="data")
    assert len(list((ROOT / "jpg").glob("image_*.jpg"))) == 8189
    report = {"downloaded_at": datetime.now(timezone.utc).isoformat(), "dataset": "Oxford Flowers 102",
              "homepage": BASE, "images": 8189, "categories": 102, "files": provenance,
              "usage": "Local public-dataset evaluation. Original images are not redistributed by this project."}
    Path("sources/flowers102-provenance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Official checksums passed; 8189 images extracted", flush=True)


if __name__ == "__main__":
    main()
