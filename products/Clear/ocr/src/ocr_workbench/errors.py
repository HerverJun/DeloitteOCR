"""Actionable engine failures without changing inference settings."""


def friendly_engine_error(error):
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
