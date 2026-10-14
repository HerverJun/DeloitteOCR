from dataclasses import dataclass
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    lease_seconds: int = 45
    max_pixels: int = 80_000_000
    max_upload_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ.get("ICP_DATA_DIR", str(REPO / ".local-data"))).resolve())

    @property
    def database(self):
        return self.data_dir / "platform.sqlite3"

    def prepare(self):
        for name in ("uploads", "thumbnails", "models", "features", "datasets", "logs", "exports", "cache"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.data_dir / "cache" / "huggingface"))
        os.environ.setdefault("TORCH_HOME", str(self.data_dir / "cache" / "torch"))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
