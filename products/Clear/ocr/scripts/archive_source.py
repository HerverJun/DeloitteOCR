"""Archive exactly one Git commit and independently verify every blob."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

p = argparse.ArgumentParser()
p.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()


def git(*args):
    return subprocess.check_output(["git", "-C", str(a.repository), *args])


assert not git(
    "status", "--porcelain"
).strip(), "Source tree must be clean before archiving"
commit = git("rev-parse", "HEAD").decode().strip()
assert not a.output.exists(), "Refusing to replace a source archive"
a.output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        "git",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
        "-C",
        str(a.repository),
        "archive",
        "--format=zip",
        "--prefix=OfflineOCR-source/",
        "--output",
        str(a.output.resolve()),
        commit,
    ],
    check=True,
)
entries = {}
for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
    if entry:
        header, name = entry.split(b"\t", 1)
        mode, kind, digest = header.split()
        assert kind == b"blob", "Submodules need explicit source packaging"
        entries[name.decode("utf-8")] = digest.decode()
algorithm = git("rev-parse", "--show-object-format").decode().strip()
with zipfile.ZipFile(a.output) as archive:
    names = [n for n in archive.namelist() if not n.endswith("/")]
    assert len(names) == len(set(names)) == len(entries)
    for name in names:
        relative = name.removeprefix("OfflineOCR-source/")
        data = archive.read(name)
        header = b"blob " + str(len(data)).encode() + b"\0"
        assert (
            hashlib.new(algorithm, header + data).hexdigest() == entries[relative]
        ), relative
with a.output.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
report = {
    "passed": True,
    "archive": a.output.name,
    "commit": commit,
    "files": len(entries),
    "bytes": a.output.stat().st_size,
    "sha256": digest,
    "method": "Every ZIP file independently compared to its committed Git blob",
}
(a.output.parent / "source-verification.json").write_text(
    json.dumps(report, indent=2), "utf-8"
)
a.output.with_suffix(".sha256").write_text(
    digest + "  " + a.output.name + "\n", "ascii"
)
print(json.dumps(report))
