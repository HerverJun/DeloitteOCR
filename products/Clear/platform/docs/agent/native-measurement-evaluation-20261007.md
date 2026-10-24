# 真实原生 OCR Agent 路线测量

`scripts/evaluate_screen_agent_native.py` 默认只准备固定输入、配置及源码 SHA；仅显式 `--run` 才启动隔离原生队列、推理和真实主控。输入及原生与平台源码均固定在 F 盘。准备阶段不读取真值，不使用冻结识别回执，不采样设备，不启动任何 GPU 服务。

实际运行沿用主基准的只读 `nvidia-smi` 查询：设备名称、已用与总显存、GPU 利用率。独立线程每 3 秒采样，使用 `CREATE_NO_WINDOW`，并在自身 `finally` 停止；`gpu-samples.json` 保留原始观测，`gpu-device-summary.json` 记录离散采样峰值。它是包含其他应用的设备总占用，不能称为本进程或连续峰值。

`stage-timestamps.json` 记录服务启动与就绪、平台输入登记、Agent 开始与终态、首次原生最终结果的观察、后续证据采集与导出、清理。首次结果保留原生记录时间及 1 秒查询粒度下的观察时间；首次实际 Excel 成果由生产导出操作的结束时间单独记录，两者不同。`agent_terminal_wall_seconds` 是提交到运行终态的墙钟时间；旧的 `wall_seconds` 字段仍包括之后的证据采集，并明确标注范围。

`platform-operation-timings.json` 保留生产工具的起止记录，`native-task-timings.json` 保留原生任务的开始、结束、之前的活跃片段秒数、原始 `elapsed_seconds`、模型加载、推理、共享上下文与缓存命中，以及每图准备耗时。原生表和完整原始结果另外保存在 `native-receipts`。CPU 准备、GPU 作业、主控等待与导出可能重叠，不能把这些字段求和称为完整路线耗时；共享上下文或缓存来源耗时也不能重复记入每次消费。供应商没有提供独立思考计时，不从 HTTP 墙钟推断思考耗时。

实际策略与冻结 `screen-policy.json` 的全部键逐一比较；只对回退引擎列表排除已选主引擎的原生规范化规则作同等处理。ZIP 的 SHA、实际 XLSX 数量、清单项目与待复核文件数量独立记录。`verified_delivery` 表示来源和字节已核验；未经人工复核的文件仍是候选，交付完整性仍可能是 `partial`。
