# Clear 统一开发源码

**唯一开发主线：`F:\OCR` 的 `main`。** 平台、分类和原生 OCR 均在本目录维护；此前分散在 `output` 中的开发仓只保留为历史来源，不再作为后续修改和同步入口。

| 本仓目录 | 维护内容 |
|---|---|
| [platform](platform/) | 门户、当前 Harness/Agent、屏拍专项接入、文档结构与主题对比、AIGC、数据处理和取证服务 |
| [classification](classification/) | 图像标注、训练、已有模型推理与人工复核 |
| [ocr](ocr/) | 原生 OCR、MinerU Pro、屏拍批次、诊断与精细候选、语言路由、透视格和单元格复核 |

整合保留 0.20.0 的连续执行和文件交付行为，以及 0.18 文档主题对比、0.19 AIGC 的实验身份。旧专项的业务实现适配当前主循环，不恢复过时的 Agent 调度和任务计划门槛。具体修改、验证和限制见[整合记录](../../docs/platform-automation/分支整合记录-20261024.md)。

## 源码与安装程序

本目录是当前开发源码。正式安装仍由 [current.json](../../release/Clear/current.json) 单独登记；源码整合不自动切换已安装的 0.20.0 candidate-006，也不修改原业务 DATA。完整 Python、依赖、模型、GPU DLL、构建和业务资料保留在本机 F 盘，不进入 Git。

根目录 `src/`、`frontend/` 继续提供独立 OCR 的兼容入口，屏拍后续实现同步收齐；Clear 全套应用的开发入口以本目录为准。准备发行时，打包器的 `--source`、`--ocr-source`、`--classification-source` 应分别指向这里的三个同级目录，不再从旧工作树取代码。使用隔离 F 盘工作区验证，避免直接升级真实业务数据库。

## 来源和清单

[source-manifest.json](source-manifest.json) 记录当前集成文件的大小、SHA256 和原始导入来源。原先的平台 `1c8d345`、分类 `1be916b`、OCR `adab40a`，以及屏拍专项 `d182744` / `e7746fc` 和其未提交文件均作为来源保留；来源版本不等同于本仓当前提交。

在本机 F 盘配置好临时、缓存及应用数据目录后，从 `F:\OCR` 运行：

```powershell
python -B scripts/sync_clear_sources.py --refresh-manifest
python -B scripts/sync_clear_sources.py --verify
```

现在无参数运行清单脚本只刷新本仓文件登记，不会重新用旧工作树覆盖已经合并的代码。针对集成源码的旧 `--platform-source` 覆盖操作会被拒绝。`--verify` 只读核对文件成员及字节，不调用模型，也不代表正式安装已更新。

根仓 `.gitattributes` 为本目录保留原始字节；来源仓的换行规则存为 `.gitattributes.source`。修改本目录后刷新清单，再提交对应源码和清单；单一开发分支中的 Git 记录是后续维护依据。

切换前备份、四仓完整 Git 历史、未提交源码和恢复方法在 `F:\OCR\output\branch-transition-prep-20261024-001`。旧取证失败实验保留其原始证据，不自动作为新正式功能。

使用说明见[平台手册](../../docs/Clear审计平台使用说明书.md)、[手动工具](../../docs/Clear手动工具使用说明.md)、[OCR 手册](../../docs/ClearOCR详细使用说明.md)和[架构说明](../../docs/Clear架构设计与实现思路.md)。
