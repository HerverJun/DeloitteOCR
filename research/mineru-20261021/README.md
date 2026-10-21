# MinerU 调研证据

日期：2026-10-21。报告位于 [MinerU深度调研与集成评估](../../docs/MinerU深度调研与集成评估-20261021.md)。

本轮新增文件仅写 `F:\OCR\docs` 和 `F:\OCR\research\mineru-20261021`。检查了 F 盘、OCR、research、docs 和本任务目录均为本机普通目录，无 junction/link。盘外工具仅通过现有工具渠道只读使用；纯 Python 探针使用 F 盘封存 control runtime，`-B` 禁止字节码，并由 environment.ps1 将临时和应用数据指向本任务目录。不改 HOME/CODEX_HOME。

本地映射：`products/Clear/platform` 是审阅快照，登记开发源为 `output/frontend-workbench-20261015/worktrees/platform`；`products/Clear/ocr` 对应 `output/clear-automation-dev-20261002/worktrees/ocr`。两组关键文件对照见 local-source-manifest.json。正式发行按 `release/Clear/current.json` 只读核对。

- `contract_probe.py` / `contract-probe.json`：合成输入，验证现有 Clear 契约，未运行 MinerU。
- `local-source-manifest.json`：本地关键源码身份与抽查映射。
- `environment.ps1`：仅探针环境，无安装或下载命令。
- `run-record.json`：验证范围、硬件与已知限制。

联网来源和关键固定标签路径在报告各结论旁列明。终端直接 HTTPS 获取失败，未保存上游完整快照；目录内不包含真实模型、上游代码副本或业务原件。后续 PoC 应另建实施目录，先核算剩余空间及许可条件。
