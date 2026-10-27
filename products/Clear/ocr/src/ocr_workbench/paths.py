"""Bridge standalone OCR and managed installs to the shared path policy.

Managed workers must receive the runtime package from the launcher; they never
recover by importing a historical development checkout. Standalone development
retains the machine's F-drive storage rule when the platform is not installed.
"""
import os
from pathlib import Path


def require_path(value, label='OCR path', *, role='write'):
    try:
        from packages.runtime import require_path as validate
    except ModuleNotFoundError as error:
        if error.name not in {'packages', 'packages.runtime'}:
            raise
        if os.environ.get('CLEAR_INSTALL_CONFIG'):
            raise RuntimeError('Managed OCR runtime is missing packages.runtime') from error
        requested = Path(value).absolute()
        resolved = requested.resolve()
        if requested.drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
            raise ValueError(f'{label} must remain on the local F drive: {requested}')
        return resolved
    return validate(value, label, role=role)


def require_model_mapping(link, target):
    """Only Paddle's temporary, launcher-owned model alias is permitted."""
    if os.environ.get('CLEAR_INSTALL_CONFIG'):
        from packages.runtime import require_model_mapping as validate
        return validate(link, target)
    return require_path(target, 'Paddle model directory', role='read')
