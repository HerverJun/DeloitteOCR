"""Actionable engine failures without changing inference settings."""


class EngineFailure(RuntimeError):
    def __init__(self, code, stage, message, *, accepted_write=False, retryable=False, user_action=''):
        super().__init__(message)
        self.error_code = code
        self.failure_stage = stage
        self.accepted_write = accepted_write
        self.retryable = retryable
        self.user_action = user_action


class EngineTimeout(TimeoutError):
    def __init__(self, stage):
        loading = stage == 'load'
        super().__init__('引擎权重加载等待超时；尚未开始识别。' if loading else '识别推理等待超时；原尝试已提交给引擎。')
        self.error_code = 'engine_load_timeout' if loading else 'engine_inference_timeout'
        self.failure_stage = stage
        self.accepted_write = not loading
        self.retryable = False
        self.user_action = ('查看引擎加载日志、模型安装与资源占用；核实原进程已停止后，可明确重试或选择允许的后备引擎。'
                            if loading else '先核实原任务回执和引擎进程状态；已接受请求不得自动重发，恢复或新尝试须沿用明确身份。')


def engine_error_details(error, *, stage='inference', attempt_id=None):
    """Safe public fields; private worker logs retain the original diagnostic."""
    if isinstance(error, (EngineFailure, EngineTimeout)):
        code, stage, message = error.error_code, error.failure_stage, str(error)
        accepted, retryable, action = error.accepted_write, error.retryable, error.user_action
    elif isinstance(error, PermissionError):
        code, message, accepted, retryable = 'engine_permission_denied', '引擎或资料访问被拒绝。', False, False
        action = '检查当前账号对程序、模型、输入和输出目录的权限；修复权限后再明确继续。'
    elif isinstance(error, TimeoutError):
        return engine_error_details(EngineTimeout(stage), attempt_id=attempt_id)
    elif any(term in str(error).lower() for term in ('out of memory', 'outofmemory', 'cuda_error_out_of_memory', 'resourceexhausted', 'bad_alloc')):
        code, message, accepted, retryable = 'engine_memory_exhausted', '显存或内存不足，未降低识别质量。', stage != 'load', False
        action = '核实原进程状态并释放其它资源占用；保持原输入与质量要求，再明确创建新尝试。'
    else:
        code, message, accepted, retryable = 'engine_load_failed' if stage == 'load' else 'engine_execution_failed', '引擎加载失败。' if stage == 'load' else '引擎执行失败。', stage != 'load', False
        action = '查看该尝试的引擎日志并核实运行状态；根据具体原因修复后再明确继续。'
    return {'version': 1, 'code': code, 'stage': stage, 'message': message,
            'accepted_write': accepted, 'retryable': retryable, 'user_action': action,
            **({'attempt_id': attempt_id} if attempt_id else {})}


def friendly_engine_error(error):
    if isinstance(error, (EngineFailure, EngineTimeout, PermissionError)):
        detail = engine_error_details(error)
        return detail['message'] + detail['user_action']
    message = str(error)
    if any(
        term in message.lower()
        for term in [
            "out of memory",
            "outofmemory",
            "cuda_error_out_of_memory",
            "resourceexhausted",
            "bad_alloc",
        ]
    ):
        return (
            "显存或内存不足：当前项已停止并释放引擎，未降低识别质量。关闭其他占用显存的程序，或先裁剪/缩小图片后重试。详细错误："
            + message
        )
    return message
