# 四维审计修复说明 · 2026-09-12

本轮根据 [审计汇总](audit-20260912-summary.md) 修复当前源码，保留会话开始前的未提交修复。新增代码、回归脚本与说明；未加载真实 OCR 模型、操作既有用户项目、提交 Git 或重新生成离线发布包。

## 已完成的修复

| 审计问题 | 当前行为 | 主要实现 |
| --- | --- | --- |
| 保存失败后重新加载会清空待保存草稿 | 读取成功后才替换；失败保留草稿、保存重试和离开保护。明确的放弃确认及 JSON 草稿下载。 | useEditor.ts、App.tsx |
| 一次 GET 失败后无恢复入口 | 单独的加载状态，保留目标 ID，最多两次自动重试及明确的手动重试。 | useEditor.ts、App.tsx |
| 队列退出但健康检查仍 ready | 线程存活、故障与退避状态可观察；临时错误有限重试，持续错误等待明确恢复。故障任务复核后继续，已提交成功项不重跑。 | task_queue.py、service.py |
| 查看历史结果改变采用状态 | 查看与下拉框只改变预览；只有明确采用动作修改批量导出选择。 | App.tsx、resultWorkflow.ts |
| 当前无结果阻断批量导出 | 当前预览、所选图片、整个项目三种范围，资料栏增加导出所选；缺少结果的数量明确显示。 | App.tsx、resultWorkflow.ts |
| 普通竖线文字误判成表格 | Markdown 必须有合法表头分隔行与匹配列数，代码、转义竖线及普通绝对值文字保持原文。 | tables.py |
| 表格解析或附带 Excel 失败使整个结果不可用 | 保存文字、原始 JSON 与正式结果，记录格式警告；Excel 特有限制只在该格式生成时检查。 | worker.py、editing.py、tables.py |
| 导入/版本写入失败遗留文件 | 同目录暂存、校验、发布与数据库登记，失败补偿；孤儿扫描与正在发布的图像互斥，按明确选择隔离并保留恢复清单。 | imaging.py、maintenance.py |
| 小屏工具裁切、文件名隐藏、首次无导入入口 | 按结果面板宽度换行；整表动作收在设置内；保留当前文件名和位置；空项目直接导入，暂停/失败/取消提供下一步。 | professional.css、App.tsx、TableEditor.tsx |
| 缺少人工复核状态 | 待校对/已确认/有疑问独立于任务状态；确认绑定图像版本、采用结果、revision；筛选、下一张待校对、仅导出已确认。 | store.py、service.py、PhotoList.tsx、App.tsx |
| 批量交付来源不可追溯 | 每个 Excel 附来源索引，逐图 ZIP 另有 sources.json；记录原图、版本、引擎、结果、revision 与工作表映射。 | exporting.py、tables.py |
| 比较混合条件、忽略结构变化 | 默认同图像版本和批次，显示处理条件；选择基准与原始/校对视图，对齐文字并比较单元格、行列和合并关系。 | ResultComparison.tsx、comparisonOps.ts |
| 人工结构修复与键盘通路不足 | 新建/删除整表、TSV 区域粘贴、Shift+方向键矩形选择；坐标输入、键盘微调及 Escape 取消；结果页签具有选中与方向键语义。 | TableEditor.tsx、tableEditing.ts、ImageCanvas.tsx |
| 可疑项提示缺乏明确分数来源 | 仅用当前引擎返回的有限数值，阈值可调整并导航；无分数显示未知，不宣称统一准确率。 | TableEditor.tsx、App.tsx |
| 全量轮询、历史增长和数据库回退兼容 | 查询索引、单事务快照、变化 revision；未变只返回小型状态，隐藏/空闲页面降频；图片和任务分批渲染。历史无损压缩并计入空间说明，低空间提示。高版本拒绝，旧库先备份后逐级迁移。 | store.py、maintenance.py、service.py、App.tsx |
| GPU/引擎故障阻断已有结果访问 | 显式 --review-only，仅校对、导出和普通图像处理；核心应用、service 运行时完整性与依赖仍须通过，禁用识别和引擎启用。 | startup.py、launcher.py、service.py |

复核和导出另补了并发保护：确认必须使用用户已经看到或刚保存的 revision；其他窗口有新修改时拒绝确认。项目快照中的 revision、文件名、采用值和复核状态在同一 SQLite 读取事务内获取。导出先捕获一致的内容与来源快照，生成 Excel 时不持有该读取事务，避免输出与来源索引互相错配。

## 验证结果

- **113 项 Python 测试通过**，覆盖队列故障、卸载/失败状态写入、图像暂存与登记失败、孤儿并发隔离、数据库高版本拒绝/升级回滚、压缩历史、复核、导出一致快照与仅校对服务接口。
- **19 项前端测试通过**；TypeScript 与 Vite 生产构建通过。esbuild 在沙箱内读取父目录受限，已获自动批准，在本机沙箱外完成构建及测试。
- **11 项本轮浏览器场景通过**：失败保存再失败加载、预览/采用、当前无结果时批量导出、暂停/取消恢复、跨窗口复核、键盘矩形合并、TSV/整表撤销、图像坐标键盘输入、有限加载重试、未变快照与小屏布局。浏览器脚本错误为 0。
- **10 项既有功能浏览器回归通过**：延迟撤销跨图/跨项目、明确放弃后延迟加载、迟到 GET、复制最新表格、Tab 选区与行列删除、Shift 点击合并、迟到导入及往返切换、混合表格导出与不完整 Excel 批次拒绝。脚本错误为 0。

浏览器使用外部无界面 Edge，带 `--disable-gpu --disable-gpu-compositing`，顺序运行；每轮结束关闭浏览器和隔离服务。所有图片、数据库和识别内容均为合成测试数据，不构成真实 OCR 精度证明。

| 场景 | 修复后实测 |
| --- | --- |
| 1200×800，原图区设为 65% | 结果面板 340px，表格滚动区 338px × 222px；常用按钮均在面板内，无页面横向溢出。 |
| 1366×768，资料栏和队列展开 | 表格可编辑滚动区高 171px；原报告为 115px。正常批次结束且无异常时自动收起队列。 |
| 1100×750 | 当前文件名保持可见，可悬停查看完整名称，同时显示图片位置。 |
| 1024×576，独立空项目 | 首屏可见导入图片/文件夹；实际导入后只有一张图片，显示 1/1。 |

首次全流程中的空项目截图准备曾被测试上下文的项目偏好重置，因此另用全新浏览器上下文独立复测空项目。下列最终布局证据替代该首次布局记录；其他 10 项行为断言保持有效。

本机证据：

- [行为回归记录](../build/audit-fixes-20260912-run3/audit-fixes-evidence.json)
- [最终布局与首次使用记录](../build/audit-fixes-20260912-layout-final/audit-fixes-evidence.json)
- [既有功能回归记录](../build/audit-fixes-20260912-legacy/regression-results.json)
- [窄结果面板截图](../build/audit-fixes-20260912-layout-final/narrow-result-1200.png)
- [队列展开截图](../build/audit-fixes-20260912-layout-final/queue-open-1366.png)
- [空项目首次使用截图](../build/audit-fixes-20260912-layout-final/first-use-empty-1024.png)

脚本位于 `scripts/verify_audit_fixes.py/.mjs` 与 `scripts/verify_functional_regressions.py/.mjs`。先构建 frontend，再用具备 service 依赖的 Python 执行，例如：

```powershell
& 'E:\OCR-deloitte-build\bundle\runtimes\service\python.exe' -B -X utf8 scripts/verify_audit_fixes.py --output build/audit-fixes-rerun --port 8877
& 'E:\OCR-deloitte-build\bundle\runtimes\service\python.exe' -B -X utf8 scripts/verify_functional_regressions.py --output build/functional-rerun --port 8878
```

输出目录须不存在；这两个脚本显式从当前仓库 src 导入，不会启动模型工作线程。

## 隔离规模与历史存储测量

[性能证据](../build/audit-fixes-20260912-performance/evidence.json) 与 [重跑脚本](../build/audit-fixes-20260912-performance/reproduce.py) 使用 1000 张图、1000 个版本、4000 个任务的合成项目：Store 全量快照 3,208,267 字节，未变响应 34 字节；全量查询 7 次中位 30.104ms，未变查询 25 次中位 1.063ms。34 字节只计 Store 数据，不含 API 增加的队列、磁盘状态及 HTTP 开销。EXPLAIN 确认图片、任务、版本使用项目相关索引；稳定 ID 排序的末项与跨图片版本排序仍存在临时排序。

10 万字符重复十进制文本连续 50 次微编辑，51 条历史压缩后 13,410 字节，等价未压缩内容 5,101,173 字节；SQLite 文件由 323,584 增至 520,192 字节。重开后逐步完成 50 次撤销和 50 次重做，内容全部一致，原文保留。真实文本、表格的压缩率因内容而变，不能直接套用这一比例。

## 数据与交付边界

数据库升级至 **v7**，首次升级先在工作区 `database-backups` 创建一致性备份。未知高版本在任何 WAL/schema 写入前拒绝。迁移中的 DDL、历史压缩和版本号修改使用同一事务；失败回滚。历史完整保留，压缩不会通过静默删除历史节省空间。

回退前退出所有进程并完整备份当前工作区；使用独立副本中的迁移前数据库和匹配项目文件验证旧版。旧备份不包含迁移之后的新校对，不应直接覆盖唯一工作区。

已消除静态项目每 1.2 秒重复传输全部历史的成本；活跃项目发生变化时仍返回全量快照。后续若扩大规模，可继续做服务端分页或任务差量；本轮没有宣称已完成大项目浏览器性能验收。图像及表格键盘操作已做实际浏览器验证，Narrator/NVDA 的真人专项验收仍需在目标机器进行。

`--review-only` 已在源码与隔离测试中实现，需要重新构建后随便携包交付。本轮没有更新现有发布 ZIP、启动器可执行文件或内置运行时，也没有补做另一台干净 Windows、内网 A4000、真实内网样本、四引擎推理精度/速度验收。
