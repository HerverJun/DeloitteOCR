from dataclasses import dataclass
import os
from pathlib import Path
from .paths import require_path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    lease_seconds: int = 45
    max_pixels: int = 80_000_000
    max_upload_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_env(cls):
        configured = os.environ.get('ICP_DATA_DIR')
        if os.environ.get('CLEAR_INSTALL_CONFIG') and not configured:
            raise ValueError('Managed classification requires an explicit ICP_DATA_DIR')
        return cls(require_path(configured or REPO / '.local-data', 'Classification DATA'))

    @property
    def database(self):
        return require_path(self.data_dir / 'platform.sqlite3', 'Classification database')

    def prepare(self):
        require_path(self.data_dir, 'Classification DATA')
        directories = [require_path(self.data_dir / name, 'Classification ' + name) for name in
            ('uploads', 'thumbnails', 'models', 'features', 'datasets', 'logs', 'exports', 'cache')]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.data_dir / "cache" / "huggingface"))
        os.environ.setdefault("TORCH_HOME", str(self.data_dir / "cache" / "torch"))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
