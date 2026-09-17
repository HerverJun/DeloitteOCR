# 当前项目功能深度复盘与测试

日期：2026-09-17。对象：当前工作区 **0.11.0rc1 / schema 11**，包括已有未提交代码。结论：基础识别、编辑持久化和人工采用流程已经形成较完整的闭环；跨入口导航、排队语义和历史候选状态仍有明显缺口。本轮确认 **8 项缺陷：1 项 P1、7 项 P2**。这些缺陷仍未修复。

此次只新增审计材料、运行测试与构建前端，没有修改产品源码或原有用户项目。对 145 个源码、配置与前端文件进行前后 SHA256 比较，均未变化；后端源码和配置与 D 盘当前便携包逐字节一致。所有操作使用隔离工作区。证据：[文件与导出核对](verification-summary.json)、[源码快照](source-snapshot.json)。

## 最需要先处理的问题

| 编号 | 优先级 | 已证实的用户问题 | 主要位置 | 证据层级 |
|---|---|---|---|---|
| R1 | P1 | 预览第 2 页，“处理当前页”实际处理第 1 页 | `frontend/src/DocumentTree.tsx:33,118` | 真实浏览器请求 + 组件复现 |
| R2 | P2 | 融合等带来源结果点击“复制”，写入 ZIP 乱码并提示成功 | `frontend/src/App.tsx:1203–1212` | 真实浏览器 + 实际导出文件 |
| R3 | P2 | 预览结果 A 后，文档复核队列无法切到采用结果 B | `frontend/src/App.tsx:855–859` | 真实浏览器 + 组件复现 |
| R4 | P2 | 页面展开期间提交处理，返回成功但处理请求丢失 | `src/ocr_workbench/document_store.py:228–230` | 真实 PDF / CPU worker |
| R5 | P2 | 前 50 个区域任务暂停，后续已完成页无法发布 | `src/ocr_workbench/page_processing.py:226` | 51 页持久化队列复现 |
| R6 | P2 | 文档中确实存在的引号、Windows 路径等搜索不到 | `src/ocr_workbench/document_routes.py:137` | 实际存储 + 搜索处理函数 |
| R7 | P2 | PDF 表格工具更新后，全文队列继续列出失效候选 | `src/ocr_workbench/document_review.py:45–72` | 工具版本变更复现 |
| R8 | P2 | 重复审校同一目标，文档复核队列 ID 重复 | `src/ocr_workbench/document_review.py:30` | 两轮建议 / 排队请求复现 |

P1 表示应优先阻断错误操作目标；P2 表示正常使用路径中的功能或状态缺陷。本轮没有确认原始 OCR 被上述问题改写，也没有把可能的后果写成已经发生的数据损坏。

### R1：工作区可见页面与操作页面不一致

先在文档树打开第 1 页，再从任务队列查看第 2 页结果。右侧文件名、结果选择和正文均已显示第 2 页，但文档树同时标记两页为当前页；点击“处理当前页”后，实际 POST URL 指向第 1 页。

`DocumentTree.current` 优先使用保留的 `selectedPage`，而外部跳转只更新 `activeImage`。这不是单纯高亮错误：请求目标确实错误。在整页重新 OCR 模式下，也会把非预期页面送入重处理；本轮没有声称已发生人工编辑丢失。

修复应统一页面导航上下文，并覆盖 document、page、offset、image、result 的同步。验收至少包括树→任务队列、树→下一张待校对、跨文档任务跳转，并断言 **请求 page_id 等于当前可见页**。

### R2：复制功能混用了带来源的下载接口

真实融合结果的 `/export` 请求虽然传入 `format=txt`，响应却是 `application/x-zip-compressed`。前端直接执行 `response.text()` 并写入剪贴板，得到以 `PK\x03\x04` 开头的 3,858 个字符，界面提示“文字已复制”。测试仅在内存中截取剪贴板写入参数，没有覆盖用户系统剪贴板。

下载归档附带融合、结构、多模态来源是正常设计；复制调用没有处理这种返回格式。普通单引擎无来源结果作为对照，导出仍为纯 TXT。应为复制使用明确的纯文本呈现路径，并按用户期望文字断言，不能把剪贴板与同一个错误响应相互比较。

### R3：复核条目没有携带完整的结果跳转

第 2 页已采用结果 B 有待处理的视觉审校建议。先通过任务队列预览同页的其他结果 A，再点击侧栏指向 B 的建议，界面仍加载 A、仍停留“文字”页签。真实浏览器的 expectedResult 和 actualResult 不相同。

`goTo` 只打开图像，`onReview` 只设复核目标；`previews[imageId]` 仍优先 A，复核定位钩子因结果 ID 不匹配直接退出。修复应先显式选中条目指定的 result_id，再进入对应审校页签并定位建议。应同时覆盖结构、融合、视觉审校三类入口。

R1–R3 共用的最终证据：[浏览器回执](ui-extended-verified/extended-results.json)、[错误页面截图](ui-extended-verified/navigation-wrong-page.png)、[错误复核结果截图](ui-extended-verified/review-wrong-result.png)。详见[前端分项报告](frontend/REVIEW.md)。

### R4：把“同一页面忙碌”误当成“同一请求幂等”

对真实原生 PDF 排队 render，尚未执行时再提交 process(native)。两次调用返回同一个阶段 ID；CPU 完成后仅有 `render/succeeded`，图像已生成，但结果数为 0，第二次 step 没有任何处理任务。

查询只按 page_id 与活动状态复用任务，没有区分 kind、mode、engine、force 等参数。应只合并语义相同的请求；不同操作应有明确后继关系或返回冲突，不能显示排队成功后静默丢弃。验收需覆盖 queued/running render→native、native→force OCR、不同引擎请求。

### R5：固定队列前缀导致已经完成的页面饥饿

建立 51 个等待区域 OCR 的页面，使用真实队列 action 暂停前 50 个区域任务，再完成第 51 个。反复执行页面完成检查，最后阶段仍为 `waiting_gpu`，结果数为 0；释放前面的一个槽位后，它才发布成功。

每轮只查看最早的 50 个 waiting_gpu 阶段，未完成项长期占住整个扫描窗口。应优先筛选依赖已结束的阶段，或公平轮转全部等待页，再对单轮处理量设限。此问题可跨文档、跨项目传播。复现使用合成区域识别结果，验证的是持久化调度语义，没有伪称运行了 51 页真实模型。

### R6：搜索过滤的对象是 JSON 编码文本

正文为 `合同写明 "甲方" 应付款，路径 C:\财务\报表，收款人 ÉLODIE`。搜 `甲方` 命中；搜 `"甲方"`、`C:\财务\报表`、`élodie` 均为零。引号和反斜杠在 JSON 中被转义，SQLite 默认 lower 也不等同于后续 Python Unicode casefold，正确记录先被排除。

应搜索解码后的可见正文与单元格内容，索引和最终比较使用同一规范化规则。验收应包含引号、路径、换行、中文、前导零与非 ASCII 大小写。

R4–R6 证据：[实际复现结果](backend/reproduction-results.json)、[复现脚本](backend/reproduce_backend.py)、[后端分项报告](backend/findings.md)。

### R7：同一候选在不同界面的有效性判断不一致

表格工具身份变化后，结构面板正确返回 `outdated`，候选与建议均为零；全文队列却仍显示历史结构建议，alternative 带 `can_apply=true`，且 candidate_pages、checked_pages 均为 1。重新检查后，旧待办仍存在。

采用 API 会拒绝它，错误为“表格工具候选已过期，请重新提取并检查”，修订保持 0。因此此处是可操作性和状态一致性问题，没有观察到失效候选污染内容。应复用同一套当前工具/图像/版本有效性规则，并区分历史处理记录与当前待办统计。

### R8：复核队列条目缺少请求身份

同一修订、同一目标执行两轮审校，6 条建议只产生 3 个唯一 queue.id；两个尚未执行的审校任务也共享 ID。底层 proposal_id、task_id 正常唯一，但队列 ID 只包含页、结果、类型、目标。

前端直接用 queue.id 作为 React key，不满足列表身份要求。本轮确认接口重复 ID，未声称已复现 DOM 丢项或错误内容写入。应显式按目标分组，或为独立建议/任务加入 proposal_id/task_id。

R7–R8 证据：[复现回执](review-features/review-features-receipt.json)、[复现脚本](review-features/probe_review_features.py)、[分项报告](review-features/REPORT.md)。

## 本轮实际执行的测试

| 测试层 | 当前结果 | 能支持的结论与限制 |
|---|---|---|
| Python 全量 | 383 项：382 通过、1 跳过，146.977 秒 | 当前源码回归；跳过项需要独立 GriTS 评分环境 |
| 前端 Vitest | 39/39 通过 | 主要是纯函数与合约测试 |
| TypeScript + Vite | 通过 | 仍有 592.80 kB 主 JS 分块体积提示，不作为功能缺陷 |
| 多模态浏览器 | 9/9 场景通过 | 可用性、选择、丢响应重放、采用、撤销、导出、取消重试、小屏；模型响应为明确合成夹具 |
| 文档浏览器 | 10/10 场景通过 | 跨页保存、PDF 预检/选页下载、普通搜索、TIFF、密码、快速校对、人工定位 |
| 结构浏览器 | 6/6 场景通过 | 替换/补行、文字保留、撤销重做、跨页暂缓、来源恢复、窄屏 |
| 工具恢复浏览器 | 4/4 场景通过 | 工具超时可见、独立重试、编辑保留、刷新后状态持久 |
| 新增浏览器缺陷探针 | 3/3 成功复现缺陷 | R1–R3；这些是确认失败行为，不算功能通过 |
| 新增基础链路控制 | 7/7 组通过 | 中文路径、原图完整性、共用预处理、保存/历史/导出、确认失效、项目隔离 |
| 新增编辑异步控制 | 4/4 组通过 | 慢保存期间继续编辑、失败保留草稿、恢复修订冲突、乱序读取防护 |
| 新功能定向回归 | 78/78 通过 | 与全量测试重叠，不能再加成独立测试总数 |
| 四引擎真实 GPU | PP-OCR / PaddleVL / GLM / Hunyuan 全成功 | 各 1 次表格夹具，长编号正确保留；不能得出独立准确率或持续吞吐结论 |
| 真实视觉审校 | 可靠定位、无定位、运行中取消三组均运行 | 详见下节，包含真实错误建议 |

浏览器为外部 Playwright/Edge，顺序运行，使用 `--disable-gpu --disable-gpu-compositing`。29 个正常场景最终均通过，最终回执没有 JavaScript 页面异常。浏览器均关闭，没有使用 Codex 内置浏览器。

初次旧文档脚本执行到 TIFF 时，因为新增复核队列也包含“第 2 页”按钮，宽泛 locator 出现 strict mode 冲突。保留原始失败于 `ui-document`；只在审计副本里把 locator 限定到 `.document-pages` 后完成全部场景，未修改产品代码或原脚本。新增缺陷探针编写阶段的定位器/语法失败也保留，最终证据为 `ui-extended-verified`。

正常场景回执：[多模态](ui-multimodal/ui-report.json)、[文档](ui-document-scoped/ui-results.json)、[结构](ui-structure/report.json)、[工具恢复](ui-tool-recovery/report.json)。完整日志：[Python](backend-tests.log)、[前端](frontend-tests.log)、[构建](frontend-build.log)。

## 模型与实际交付能力复盘

四引擎使用当前便携包实际启动器、HTTP 服务和独立引擎运行环境。首次启动完整文件哈希、驱动与依赖检查通过，用时 138.625 秒；中文空格数据路径、鉴权、拒绝跨源访问、GPU 任务串行、空闲释放、正常退出及令牌清理通过。后端和配置已核对与当前源码一致。详见 [四引擎回执](four-engines/application-audit.json)。

| 引擎 | 加载耗时 | 表格夹具识别耗时 |
|---|---:|---:|
| PP-OCR | 6.29 秒 | 0.80 秒 |
| PaddleOCR-VL | 9.72 秒 | 3.73 秒 |
| GLM-OCR | 2.94 秒 | 5.01 秒 |
| HunyuanOCR | 3.02 秒 | 1.31 秒 |

这些是单次样本观察。quick 脚本还会输出“resident session reused”，但每引擎只有一个样本，这个断言没有验证跨任务复用，本报告不把它计为复用能力证据。

本机为 RTX 4070 Ti SUPER 16GB；视觉审校默认本地 Qwen3.5-4B。输入为明示的合成中文/数字表格，OCR 中人为植入 3 个错误。

| 情形 | 本次观察 | 队列执行耗时 / 整张设备峰值 |
|---|---|---|
| 可靠文字框 + 人工单元格框 | 9 目标，6 保留、3 修改，3 项植入错误均修正 | 12.52 秒 / 6,251 MiB |
| 删除全部局部定位 | 9 目标，5 保留、4 修改；除修正 3 项外，还错误建议把“数量”改成“00075” | 18.79 秒 / 8,169 MiB |
| 模型首次请求时取消 | cancelled，零条建议，内容不变，工作线程结束 | 总计 2.46 秒；取消至线程结束 0.28 秒 |

证据：[可靠定位](gpu-review/receipt.json)、[整页回退](gpu-full-page/receipt.json)、[取消](gpu-cancel/receipt.json)。推理完成时编辑和原始内容保持不变；测试脚本随后在隔离合成工作区模拟人工采用与撤销，不代表产品自动采用。模型用时包含队列内加载/退出，不含脚本之前的全量权重校验；显存包含桌面占用。

**本次重新观察到了“运行成功，但建议错误”。** 多模态能力应继续作为人工辅助。是否有可靠定位，应成为用户理解建议可信边界的核心信息；“修改了几条”不能当作“纠对了几条”。这项观察作为模型限制单列，不混入上面的 8 项程序缺陷。

历史 TableFormer/local-v3 报告中，实际结果轨完整格覆盖为 69.71%、合并格覆盖为 43.75%，低于原定门槛。这些是历史质量证据，本次未重跑，不能把本轮工程测试通过解释为定位能力达到生产阈值。历史来源：`docs/tableformer-next-20260915-quality.md`。

## 为什么现有测试全部通过仍能找到这些问题

1. **状态的单一来源尚未贯穿界面。** image、page、selectedPage、preview、adopted result、review target 分别维护，各组件局部合法，组合后指向不同对象。R1、R3 由此产生。
2. **请求与结果的契约不够显式。** 同页忙碌被误当成幂等，TXT 格式请求被误当成纯文本响应。R2、R4 都需要对调用方承诺真实行为。
3. **新功能的失效规则未被所有读取入口复用。** 结构面板与全文队列的候选有效性、唯一身份不同，导致 R7、R8。保存时阻止错误写入已有防护，读取和导航层仍需同等一致性。
4. **规模边界不能只用增加任务数验证。** R5 需要“前缀暂停 + 后续完成”的特定组合；跑很多正常顺序任务不一定触发。
5. **测试预期有时沿用了实现输出。** 既有复制测试把剪贴板与同一个 `/export` 的 `.text()` 比较，双方都为乱码仍可能通过。正确预期应来自已知校对文本。旧 UI 文档脚本未限定按钮所在区域，也会因功能增加而失效。

项目已有较好的原始结果不可变、修订冲突、草稿恢复、建议人工采用、撤销和来源记录基础。下一阶段最有价值的工作，是把这些保障延伸到整个用户操作路径，而不是只增加模型入口或正常场景数量。

## 建议的修复与验收顺序

第一批关闭 **R1、R2、R3、R4**：统一导航，明确复制与下载契约，纠正页面阶段的幂等边界。每个修复保留本轮最小反例，并增加相邻的正常路径控制。

第二批关闭 **R5、R7、R8、R6**：公平扫描完成队列、统一当前候选有效性与统计、固定唯一身份、搜索解码后的内容。队列验收至少包含跨文档、跨项目和批量暂停/继续组合。

之后固定一套用户任务链作为每版门槛：导入 → 展开/处理 → 中途切页 → 多结果预览 → 从复核队列回到采用结果 → 人工修订 → 撤销/恢复 → 复制 → 带来源导出。断言实际目标 ID、修订、可见文字、导出读回一致；网络丢响应和工具版本变化也进入该链路。

产品质量验收另行建立独立真实材料集，分别统计识别错误、错误修改、弃权、定位覆盖和人工复核耗时。本轮没有评测另一台干净 Windows、目标 RTX A4000、系统级禁网、连续数小时压力、断电恢复或新的独立准确率集，不能据此给出这些范围的发布结论。

## 复跑入口

在仓库根目录执行，使用便携包服务运行时，避免系统 Python 缺少依赖。新 UI/GPU 实验必须使用全新的输出目录。

```powershell
$auditPython = 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe'
& $auditPython -B -X utf8 -c "import sys,unittest;sys.path[:0]=['src','tests'];r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"
& $auditPython -B -X utf8 audit/deep-review-20260917/backend/reproduce_backend.py
& $auditPython -B -X utf8 audit/deep-review-20260917/review-features/probe_review_features.py
node audit/deep-review-20260917/frontend/repro-components.mjs
& $auditPython -B -X utf8 audit/deep-review-20260917/run_document_probe.py audit/deep-review-20260917/extended-ui.mjs audit/deep-review-20260917/reproduce-new-ui
```

缺陷探针当前以“成功复现缺陷”为通过条件；修复后应反转相关断言，不能继续用探针退出码 0 代表产品正确。
