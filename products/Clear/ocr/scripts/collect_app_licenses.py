"""Collect frontend and Qt notices, exact source archives and build metadata."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

parser = argparse.ArgumentParser()
parser.add_argument("--bundle", type=Path, required=True)
parser.add_argument("--frontend", type=Path, required=True)
parser.add_argument("--launcher-env", type=Path, required=True)
args = parser.parse_args()
dest = args.bundle / "licenses"
records = []
lock = json.loads((args.frontend / "package-lock.json").read_text("utf-8"))
for relative, info in lock["packages"].items():
    if not relative:
        continue
    source = args.frontend / relative
    if not source.is_dir():
        if info.get("optional"):
            continue
        raise FileNotFoundError(source)
    name = relative.removeprefix("node_modules/").replace("node_modules/", "nested/")
    copied = []
    for file in source.iterdir():
        if file.is_file() and any(
            key in file.name.upper() for key in ["LICENSE", "NOTICE", "COPYING"]
        ):
            target = dest / "frontend" / name / file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            copied.append(target.relative_to(args.bundle).as_posix())
    records.append(
        {
            "package": name,
            "version": info["version"],
            "license": info.get("license"),
            "dev": info.get("dev", False),
            "files": copied,
        }
    )
font_notice = args.frontend / "public/brand/fonts/OFL.txt"
if font_notice.is_file():
    target = dest / "frontend/NotoSansSC/OFL.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(font_notice, target)
    records.append({
        "package": "Noto Sans SC",
        "version": "variable wght 100-900",
        "license": "OFL-1.1",
        "dev": False,
        "files": [target.relative_to(args.bundle).as_posix()],
    })
(dest / "frontend-index.json").write_text(
    json.dumps(records, ensure_ascii=False, indent=2), "utf-8"
)
shutil.copy2(
    args.frontend / "package-lock.json",
    args.bundle / "locks/frontend-package-lock.json",
)
for name in ["pyside6_essentials-6.10.2.dist-info", "shiboken6-6.10.2.dist-info"]:
    source = args.launcher_env / "Lib/site-packages" / name
    target = dest / "launcher" / name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "METADATA", target / "METADATA")
    shutil.copytree(source / "licenses", target / "licenses", dirs_exist_ok=True)
sources = []
for archive in (dest / "source").glob("*.tar.gz"):
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    sources.append(
        {
            "path": archive.relative_to(args.bundle).as_posix(),
            "sha256": digest,
            "tag": "v6.10.2",
        }
    )
    # Read license members only; never unpack arbitrary archive paths.
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isfile() and "/LICENSES/" in member.name:
                target = dest / "Qt-licenses" / archive.stem / Path(member.name).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
(dest / "qt-sources.json").write_text(json.dumps(sources, indent=2), "utf-8")
print(
    json.dumps({"frontend_packages": len(records), "qt_source_archives": len(sources)})
)
