# 后端基础流程深度复盘（2026-09-17）

审计对象：当前未提交的源码状态；没有修改产品源码或用户数据。复现使用便携包 service Python 加载当前仓库 `src`，临时工作区通过 `TemporaryDirectory` 独立创建并清理。没有启动 GPU、浏览器或后台常驻服务。

复现入口：`reproduce_backend.py`；执行证据：`reproduction-results.json`。命令：

```powershell
& 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe' -B -X utf8 audit/deep-review-20260917/backend/reproduce_backend.py
```

## B1 · P2 · 页面展开期间提交处理，接口成功但处理请求丢失

- 主要位置：`src/ocr_workbench/document_store.py:228–230`。
- 可达 UI：`frontend/src/DocumentTree.tsx:73` 点击未展开页提交 render；`:117–121` 处理当前页/全文仅在 HTTP 操作期间禁用，不等待 render 阶段结束。
- 复现：导入真实 `build/document-workflow/fixtures/native.pdf`；为第一页排队 render；在该阶段仍 queued/running 时调用 `Documents.process(page, 'native')`；顺序执行 CPU worker `step()` 直到没有工作。
- 实际：两个操作返回相同 ID，唯一阶段为 `kind=render,status=succeeded`；页面图片已经存在，但 `results=0`。UI 可以显示“已加入页面处理队列”，没有任何 process 阶段被执行。相同逻辑还会吞掉正在处理时提交的不同模式/引擎参数与 force 请求。
- 预期：仅相同操作和参数可幂等复用。其他操作应排在已有阶段之后，或明确返回忙碌/冲突，不能报成功并悄悄丢弃。
- 原因：查询 pending 仅按 page_id 和状态，无条件返回，跳过前面计算的 kind/parameters/version 指纹。
- 修复方向：按语义一致的 request key 复用；异类请求建立依赖或显式冲突，保持单页串行执行。

## B2 · P2 · 文档搜索对引号、Windows 路径及非 ASCII 大小写漏报

- 主要位置：`src/ocr_workbench/document_routes.py:137`。
- 复现：采用结果正文包含 `合同写明 "甲方" 应付款，路径 C:\财务\报表，收款人 ÉLODIE`，调用文档搜索。
- 实际：`甲方` 命中 1 项；`"甲方"`、`C:\财务\报表`、`élodie` 均命中 0 项；这些查询都确实存在于正文（最后一项按 casefold 比较）。
- 预期：按用户可见的已采用正文和单元格内容搜索；引用文本、文件路径与不区分大小写的名字应命中。
- 原因：数据库预过滤对 JSON 序列化的 `edited` 字符串应用 `instr`，引号/反斜杠在 JSON 中被转义；SQLite 默认 `lower` 对非 ASCII 字符不等价于后续 Python `casefold`，正确记录先被过滤，后续逻辑没有机会补救。
- 修复方向：从 JSON 解码后的实际字段搜索，且预过滤与最终匹配使用同一 Unicode 规则；若需要索引，持久化规范化搜索字段。

## B3 · P2 · 前 50 个文档阶段被区域队列暂停时，后续已完成页永久卡在等待识别

- 主要位置：`src/ocr_workbench/page_processing.py:226`（固定 `LIMIT 50`）；`:232–233` 跳过待处理阶段。
- 可达 API：`src/ocr_workbench/service.py:335–346` 支持对项目任务（包括 region_ocr）按 task_ids 暂停/继续；界面项目队列也使用这个接口。
- 复现：创建 51 个独立页面并提交 OCR，CPU 阶段全部进入 waiting_gpu；通过 `TaskQueue.action(..., 'pause', first_50)` 暂停前 50 个区域任务；正常 claim 并完成第 51 个 region_ocr；重复执行 `finalize_waiting_pages`。
- 实际：最后区域任务已 succeeded，反复完成检查后最后文档阶段仍 waiting_gpu，发布结果数一直为 0；只有恢复并完成前 50 项中的一个，再运行完成检查，最后页才 succeeded。
- 预期：独立页的完成提交不应受其他暂停页阻塞，已经完成的第 51 页应可马上校对、搜索、导出。
- 原因：每轮都只取最早 50 个 waiting_gpu，没有游标、翻页或“依赖已结束”过滤；暂停项长期占据整个窗口，饥饿还跨项目/文档传播。
- 修复方向：数据库筛选全部依赖已终结的阶段后再限量，或公平轮转全部等待阶段，并保留每轮处理上限。

## 覆盖与限制

已阅读项目说明、Store/队列/导入/编辑/导出/项目清理实现及相关现有测试；上述新增复现覆盖真实 PDF CPU render 和确定性区域任务完成流程。

另执行 `smoke_backend_contracts.py`，在同一个独立工作区串联验证 7 组实际不变量，全部通过，结果见 `smoke-results.json`：中文空格路径导入且原图哈希不变；双引擎批量预处理只创建一个共用版本；保存/重开/旧 revision 冲突/撤销/重做且原始结果不变；TXT/MD/JSON/XLSX 导出包含已保存校对、合并格、长编号与字面公式；确认后修改阻止仅确认导出且没有半成品残留；撤销后分支清除 redo；路径越界拒绝、孤儿扫描和项目清理保留其他项目。

导出单一 SQLite 快照、取消任务阻止晚到结果、图像发布失败补偿等边界已存在相应实现与回归测试，未在本次静态检查中确认新缺陷。

没有重新执行全套测试（由主审计代理统一执行）；没有对 OCR 模型质量、GPU 实际释放、1000 页真实 OCR 吞吐、故障断电与外部杀进程作出通过结论。B3 使用合成图像和合成 OCR 完成数据验证队列状态机，不声称真实模型识别质量。B1 使用项目真实 PDF fixture 与现有 CPU runtime。
