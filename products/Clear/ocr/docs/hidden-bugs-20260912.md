# 工作台隐藏问题检查与修复（2026-09-12）

本轮从用户发现的“表格正常、文字显示 HTML”出发，检查了前端编辑器、结果比较、图片与项目切换、复核、队列、表格操作，以及后端解析、保存、导出与 worker 输出。已修复下面 5 类问题。检查在独立项目和当前源码服务上进行，没有改动用户的实际项目或重新发布安装包。

## 确认的问题

| 问题 | 复现与影响 | 修复 |
| --- | --- | --- |
| 文字、搜索与已校对表格脱节 | 真实 GLM 表格将“扫描仪”改为“扫描仪（校对后）”，切到文字仍是原始 HTML，搜索新文字失败。模型比较的“保存的人工校对”也使用这份旧源码。 | 服务提供经解析的来源区间；文字视图按当前单元格生成，单个单元格的文字修改回写同一份表格。搜索、字数和保存的校对比较使用可读内容。正文区间使用 UTF-16 偏移，支持 emoji。 |
| 复制回贴会错行、丢引号 | 一个单元格含“第一行\n第二行”，另一个是字面值 `"quoted"`。复制后使用“粘贴区域”，3 行变成 4 行，引号消失。 | TXT/剪贴板输出正确转义 TSV 中的引号、制表符和换行，读取后保留原始单元格值。 |
| 前后端表格边界不一致 | 创建 1 行、1001 列的表格，界面接受但保存失败；切图触发保存重试，被无效草稿阻止。 | 创建、粘贴和增行增列均先校验后端行列与单元格限制；新建表格数量限制为 100。失败输入不会生成脏表格。 |
| 切换页签丢失正在校对的表 | 在第二张表切换“文字”后再返回，会回到第一张表。容易继续修改错误的表。 | 按结果保存当前表格索引，删除表格时收敛到有效索引。 |
| 命令行 TXT 仍输出表格源码 | worker 直接将模型原始内容写进 `result.txt`，与 GUI 的 TXT 转换行为不同。 | worker 的 TXT 使用同一纯文本导出函数，JSON、原始模型输出和 Markdown 仍保留各自格式。 |

删除结构后的保留原文仍能转换为可读文字；无法解析或超出尺寸限制的片段继续保留，不强行展开或删除。字面代码块不会当作表格解释。文字视图支持正文、标题和单个单元格校对；跨单元格、增删行列与合并关系需要在表格页签修改，界面会给出提示。

## 验证范围

内置浏览器真实操作涵盖：真实 GLM 表格转文字与搜索、文字回写单元格、撤销重做、切图保存恢复、HTML/Markdown 混排双表、emoji 正文修改、表格位置恢复、复制与实际 Ctrl+V 回贴、1001 列拦截、无效输入后切图、旧版本确认保护、定位原图、输入后立即确认、编辑导致确认失效、项目切换与筛选重置、长编号恢复、混合已识别/未识别批量导出、筛选外已选提示、纯文字默认 TXT、单任务提交/暂停/继续/取消，以及模型比较中的已保存文字。

文件选择器的内置自动化接口超时，未将该步骤记为浏览器导入通过。随后通过实际 HTTP multipart 上传一份正常图和一份坏图，确认正常图入库、坏图单独报错，并在内置浏览器中确认正常图可见。未更改浏览器或系统安全设置，也未重启 Codex。

- **155 项 Python 测试通过**，覆盖解析、导出、编辑、复核、队列错误恢复、融合及服务等；日志中的坏图、数据库锁和模拟卸载异常是故障注入测试。
- **32 项前端测试通过**，包含新增的双向文字映射、区间移动、重复表格、代码保护、边界拒绝与表格删除后重新映射。
- **13 项真实 HTTP 检查通过**：四种格式导出、最新校对内容、原始证据、XLSX 合并及长编号、TSV 换行引号回读、批量 ZIP、过期修订拒绝、部分图片导入失败。
- TypeScript/Vite 构建通过。仍存在既有的 bundle 大于 500 kB 提示。

真实 GLM 结果来自仓库已有推理证据 `audit/airgap-release/inference/00-glm-table/result.json` 与 `fixtures/table.png`。混排、换行、未识别和旧版本用例为明确构造的边界样本。此次没有重跑四引擎 GPU 推理，不能据此推断识别准确率或最终安装包已更新。融合主要由现有 Python 测试覆盖，本轮没有重新走完整的 GPU 识别加融合浏览器流程。

## 证据与复现

- `audit/hidden-bugs-20260912/before-text.png`：修复前文字页签展示 HTML 源码。
- `audit/hidden-bugs-20260912/after-text.png`：最终构建的文字视图。
- `audit/hidden-bugs-20260912/python-tests.txt`、`frontend-tests.txt`、`frontend-build.txt`：最终测试和构建日志。
- `audit/hidden-bugs-20260912/exports/verification.json`：实际 HTTP 检查结果；同目录保留四种实际导出样本。
- `build/hidden-bugs-20260912-baseline`、`build/hidden-bugs-20260912-fixed`：独立测试数据库与种子信息，不纳入发布包。

启动可复现环境（先构建 frontend；Python 需有当前服务依赖）：

```powershell
& 'E:/OCR-deloitte-build/bundle/runtimes/service/python.exe' -B -X utf8 scripts/serve_workbench_audit.py --output build/hidden-bugs-new-run --port 8879
```

打开服务后使用测试链接 `http://127.0.0.1:8879/#token=isolated-workbench-audit`。同一测试目录可用 `--resume` 恢复已保存的测试编辑。HTTP 导出验证：

```powershell
& 'E:/OCR-deloitte-build/bundle/runtimes/service/python.exe' -B -X utf8 scripts/verify_hidden_bug_exports.py --seed build/hidden-bugs-new-run/seed.json --output build/hidden-bugs-new-run/export-evidence
```

该服务不启动模型 worker，队列测试验证提交和状态控制，不伪装成完成了模型推理。
