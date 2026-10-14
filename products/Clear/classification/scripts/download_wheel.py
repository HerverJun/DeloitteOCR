"""Download a large public wheel with bounded parallel ranges and a mandatory SHA256."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from pathlib import Path
import time
import urllib.request


def download(url, destination, size, expected, workers=8):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and digest(destination) == expected:
        print("Already verified", destination, flush=True)
        return
    parts = destination.parent / (destination.name + ".parts")
    parts.mkdir(exist_ok=True)
    chunk = 8 * 1024 * 1024
    ranges = [(offset, min(offset + chunk, size) - 1) for offset in range(0, size, chunk)]

    def fetch(bounds):
        start, end = bounds
        path = parts / f"{start:012}"
        if path.exists() and path.stat().st_size == end - start + 1:
            return end - start + 1
        for attempt in range(4):
            try:
                separator = "&" if "?" in url else "?"
                request = urllib.request.Request(url + f"{separator}icp_range={start}", headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise ValueError("Server returned an unexpected range")
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise ValueError("Truncated range")
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                temp.replace(path)
                return len(data)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, 5))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(fetch, bounds) for bounds in ranges]):
            completed += future.result()
            print(f"Downloaded {completed / 1024**2:.0f}/{size / 1024**2:.0f} MiB", flush=True)
    temporary = destination.with_suffix(".assembling")
    hasher = hashlib.sha256()
    with temporary.open("wb") as target:
        for start, end in ranges:
            data = (parts / f"{start:012}").read_bytes()
            hasher.update(data)
            target.write(data)
    if hasher.hexdigest() != expected:
        raise ValueError("SHA256 mismatch; the assembled file was not promoted to an installable wheel")
    temporary.replace(destination)
    print(f"SHA256 verified: {expected}", flush=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("destination")
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    download(args.url, args.destination, args.size, args.sha256, args.workers)
