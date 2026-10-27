"""Clear's standard-library-only installation and storage contract."""
from .config import (InstallConfig, load_install, current_install, require_path,
                     require_model_mapping, storage_environment, resource_path)

__all__ = ['InstallConfig', 'load_install', 'current_install', 'require_path',
           'require_model_mapping', 'storage_environment', 'resource_path']

# Opt-in isolation is inherited by the isolated workers through managed launch
# environment. It is evidence for validation, not a replacement for OS ACLs.
import os as _os
if _os.environ.get('CLEAR_IO_GUARD') == '1':
    _config = current_install()
    if _config is None:
        raise ValueError('CLEAR_IO_GUARD requires a managed installation')
    from .io_guard import install as _install_guard
    _install_guard(_config)
