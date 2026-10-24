> 当前登记（2026-10-24）：正式8879为Clear 0.20.0易用性版candidate-006，沿用原工作区。审阅源码来自登记工作树HEAD `1c8d3459a0c4b179835b7457dc585ed9db9c91a9`，保留0.18.0文档主题对比和0.19.0 AIGC实验源码；正式发行来自独立HEAD `073a5325e2be8415e48a626a06a1056e3fe49ae0`，不合入上述实验。本目录包含2,080个登记源码文件，来源以[source-manifest.json](source-manifest.json)为准，运行身份以正式登记及候选清单为准。实用检查、正式启动、旧会话/下载及回退见[本轮实施记录](../../docs/platform-automation/Clear易用性优先与Agent简化实施-20261023.md)。

# Clear 三应用源码与版本对应

本目录把当前 Clear 的平台、分类和原生 OCR 自有工程文件集中到同一 GitHub 仓库，便于审阅、检索和后续开发。最新捕获日期为 2026-10-24；**这是带来源身份的源码快照，不是完整离线安装目录，也不是三个原开发仓库的 Git 历史迁移。** 原目录、正式运行服务和业务数据均保留。

| 本仓目录 | 原开发目录 | 本轮文件数 | 作用 |
|---|---|---:|---|
| [platform](platform/) | `F:\OCR\output\frontend-workbench-20261015\worktrees\platform` | 950 | 门户、Agent 工作台、文档主题对比、整图 AIGC 检测、Harness 适配与业务服务 |
| [classification](classification/) | `F:\OCR\output\clear-automation-dev-20261002\worktrees\classification` | 195 | 图像标注、训练、模型、推理与人工复核 |
| [ocr](ocr/) | 同登记开发根 `worktrees/ocr` | 935 | Clear 对应的原生 OCR 源码、前端、配置、测试和许可，含 MinerU Pro 接入 |

每仓 HEAD、分支、工作区状态以及全部快照文件的 SHA256/大小见 [source-manifest.json](source-manifest.json)。快照读取当前工作区字节。本轮平台 HEAD 为 `1c8d3459a0c4b179835b7457dc585ed9db9c91a9`，OCR 为 `adab40a77ad294a5011f1212491dd4277ed8d772`，分类为 `1be916be64fcbe425cae356f28c106c586da3d7a`，捕获时三仓均干净。来源仓库和远端配置保留。

当前正式入口为 **0.20.0易用性版candidate-006**、<http://127.0.0.1:8879/>。本轮审阅源码包含文档/Pro 接入、真实 Debug 修复、[文档主题对比](../../docs/platform-automation/Clear文档主题对比实施与验收-20261022.md)和[独立整图 AIGC 检测](../../docs/platform-automation/整图AIGC检测实施与真实评测报告-20261023.md)，其中主题对比与AIGC实验未纳入本轮正式发行。AIGC 保留 SAFIRE 局部定位，最坏变换误报率的单侧 95% 上界为 1.88%，超过 1% 目标，且缺少生成文档正例，保持实验能力；具体范围与数据缺口见实测报告。此前 0.14.0 的通用 Agent 能力、旧会话/成果下载、冷备和回退历史证据见[通用能力实施与验收](../../docs/platform-automation/Agent通用能力恢复实施与验收-20261019.md)。Agent 的开放分析仍使用官方 DeepSeek Harness 内核。下述较早候选与预览记录保留作历史来源，当前登记以 `release/Clear/current.json` 为准。

新版前端的参考来源、改动与本机预览见[改版记录](../../docs/platform-automation/前端工作台改版与预览-20261015.md)。最新[门户首页补齐](../../docs/platform-automation/门户首页评估与补齐-20261015.md)将默认入口改为导航门户，AI 工作区独立为 `/agent`，保留旧会话链接；本轮 29 项定向回归、23 项首页和 21 项工具目录浏览器检查通过。预览首页为 <http://127.0.0.1:8910/>，正式入口继续使用 8879。

此前取证稳定性功能、工程验收范围与回退见[取证稳定性候选说明](platform/docs/forensics-stability-20261014.md)。该阶段独立完整候选在本机 `F:\OCR\output\forensics-stability-20261014\delivery`，当时正式入口为 0.10.0；合成评测通过不代表业务准确率已验收。

最新多人任务中心见[实现记录](platform/docs/task-center-implementation.md)、[运维说明](platform/docs/task-center-operations.md)与[验收报告](../../docs/platform-automation/Clear多人任务中心实施与验收-20261015.md)。候选在 `F:\OCR\output\task-history-multiuser-20261015\delivery-v2`，共享模型/运行时，不切换正式入口。

## 与根仓和发行版本的区别

- 根仓 `src/`、`frontend/` 是独立 OCR 研发线，当前 schema 18，含较新的屏拍等功能。
- 本目录 `ocr/` 是 Clear 原生 OCR 开发线。正式运行源码取自 `current.json` 选择的0.20.0 candidate-006根 `ocr/`，保留0.17.1文档及MinerU Pro改动；0.10.0 目录继续提供已登记的共享运行时/模型。运行身份须以各候选逐文件清单核对。
- 正式 Clear 指针在 [current.json](../../release/Clear/current.json)。运行时 `launch.py` 从发行根的 `platform`、`ocr`、`classification` 加载代码，OCR 网页由发行根 `ocr/frontend/dist` 提供；`program/bundles/ocr` 中旧 `src/web` 是底层资产，不是实际界面基线。
- `release/Clear/versions/` 里的完整离线安装目录及 `output/` 下候选封包留在本机并忽略。Python、模型、GPU DLL、依赖、样本、编译产物、业务工作区和大型 ZIP 不随 Git 上传。GitHub 提供可审阅源码及交付记录，不能直接替代完整离线介质；资产身份、兼容要求和完整包证据仍由登记与交付文档说明。

## 捕获与校验

入口脚本为 [sync_clear_sources.py](../../scripts/sync_clear_sources.py)。它从原仓 `git ls-files --cached --others --exclude-standard` 取得文件清单，读取当前工作区字节，排除运行数据、模型、依赖、缓存、构建、历史 audit/research 以及可执行大包，记录来源 HEAD 和 dirty 状态。许可证、前端品牌字体、配置、测试和必要静态源资产随源保留；例如 OCR 的 NotoSansSC 字体及其许可属于前端构建源资产。

```powershell
# 先按项目存储规范设置 F 盘 TEMP/TMP/LOCALAPPDATA/APPDATA。
# 从 F:\OCR 运行；原开发仓须存在。
python -B scripts/sync_clear_sources.py --platform-source F:\OCR\output\frontend-workbench-20261015\worktrees\platform
python -B scripts/sync_clear_sources.py --verify
# 如需单独捕获此前取证稳定性隔离源：
python -B scripts/sync_clear_sources.py --platform-source F:\OCR\output\forensics-stability-20261014\worktrees\platform
```

`--verify` 只读检查清单成员的字节和自有文件清单，忽略规则排除的运行缓存；不运行模型，不读取业务工作区，也不证明发行包与来源 HEAD 完全一致。重新捕获会覆盖同名快照源文件，不移动或删除原仓，也不会自动清除旧快照中的多余文件；变更源文件范围时应审阅 Git diff 与清单差异。不要把捕获动作当作发布切换。

根仓 `.gitattributes` 对本目录关闭换行转换，保证 GitHub 克隆后能够重新核对清单哈希。源仓自身的 `.gitattributes` 原字节保存为 `.gitattributes.source`，避免其中的换行规则覆盖快照策略；每个清单成员的 `source_path` 记录原相对路径。编辑快照文件后，其原始来源哈希自然会过期，必须说明实际改动并重新登记，不能直接沿用旧清单宣称来源一致。

## 开发与运行思路

平台与分类各自保留 `pyproject.toml`、前端 `package.json`/锁文件和测试；OCR 保留前端锁文件与离线运行时/模型锁。开发时使用 F 盘独立环境与 F 盘隔离工作区，按各工程配置构建，不复用真实业务数据库。原工程中的固定历史路径仅作来源追溯，运行前统一到 F 盘实际目录。

本次没有改写既有启动器对原目录的绑定，没有把生产入口切换到这个快照，也没有新增下载或安装模型。完整启动依赖仍需已校验的离线运行资产及原生登记；仅 `pip install` 平台基础依赖不能得到四引擎 OCR 或新增多模态模型环境。

面向使用者请从[平台使用说明书](../../docs/Clear审计平台使用说明书.md)开始；面向开发者见[架构设计与实现思路](../../docs/Clear架构设计与实现思路.md)。
