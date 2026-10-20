"""Public business errors use authored messages, never native exception text."""
from .identity import Conflict
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeNotSubmitted:
    """A native authoritative negative receipt for one stable request key."""
    operation_id: str
    tool: str
    request_id: str
    project_id: str
    input_fingerprint: str


class ResourceBusy(Conflict):
    """A concrete competing run owns the native object; no write was sent."""


class DirectActionFault(ValueError):
    def __init__(self, message, *, code='direct_action_invalid', user_action='请核对所选资料、参数和当前服务状态。'):
        super().__init__(message)
        self.code = code
        self.user_action = user_action


class NativeReaderUnavailable(ValueError):
    """A fixed read could not restore its existing linked native service."""
    def __init__(self, application, *, cause_code='APP_UNAVAILABLE'):
        super().__init__('原成果保留，读取所需的本地服务暂不可用；恢复服务后可继续，未重新计算')
        self.application, self.cause_code = application, cause_code
