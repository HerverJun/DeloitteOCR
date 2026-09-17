# 通用视觉审校第一阶段 · 0.11.0rc1 验证记录

2026-09-17，schema 11。已实现本地多模态二轮审校、持久化任务、人工采用与撤销、来源记录和校验清单输出。默认使用官方基础模型 Qwen3.5-4B 的 Unsloth Q4_K_M 转换，配 F16 视觉组件；模型和业务接口分离。产品运行不下载、不上传文档。

同日后续[深度审计](../audit/deep-review-20260917/REPORT.md)发现跨结果复核跳转、带来源结果复制等 8 项未修复问题。本文件保留该阶段原始验证记录；当前状态需同时阅读后续审计，不能据此认定所有组合流程均已通过。

本轮证据支持“可运行、可复核、可撤销”的人工辅助流程，不能据此声称达到独立准确率门槛。没有可靠局部定位、跨栏文本和密集数字行仍会引错，模型建议始终需要人工选择。

## 完成的行为

- 对选中文字、逻辑单元格或本页现有文字与单元格审校。图像、采用结果、修订、原值、配置与几何证据在入队时固定；表格源码不重复当正文输入。
- 与既有 OCR 共用单 GPU 队列，推理前卸载原引擎；支持取消、失败重试、重启后恢复。完成或取消后回收模型进程和 GPU 锁。
- 模型仅输出视觉转录 `reading` 或明确弃权的 `null`。程序精确比较转录与原值，生成 `keep/replace/uncertain`；前导零、标点、空格及空字符串都按字面处理。
- 独立建议表保存模型结果；用户采用才写入可撤销的编辑历史。原始 OCR 不变，建议不参与多引擎投票，也不自动确认整页。
- 同任务内无冲突建议可连续处理；普通编辑、撤销、切换图像／采用结果会使旧建议过期。其他任务的旧快照也不能覆盖新修订。
- 请求丢响应可用原编号幂等恢复；即使模型资源已移除或处于仅校对模式，查询既有任务也不重复启动推理。
- JSON、Markdown、XLSX 校验清单，普通输出的审校来源附件，PDF 来源规格传递。Excel 字符串读回校验，公式样式文字不作为公式执行。
- 文字／表格快捷入口、原图对照、可用模型选择、任务记录、建议处理、复核队列跳转及导出。

使用步骤与 API 见[使用说明](multimodal-review-usage-20260917.md)，资源 revision、SHA256、许可和官方型号证据见[模型说明](multimodal-models-20260917.md)。

## 工程验证

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 修改前后端基线 | 321 项运行，1 项跳过，其余通过 | `audit/multimodal-review-20260917/baseline-tests.log` |
| 最终后端回归 | 382 项运行，381 通过、1 跳过，146.997 秒 | `audit/multimodal-review-20260917/backend-verified.log` |
| 队列快照版本守卫补充回归 | 旧队列快照不能静默使用升级后的提示词；审校运行、存储、集成 62 项通过 | `snapshot-version-verified.log` |
| 最终前端 | 39 项通过；TypeScript / Vite 构建通过 | `frontend-verified.log`；`ui/run-05` |
| 外部 Playwright / Edge | 9 项通过，无 JavaScript 异常 | `ui/run-05/ui-report.json` |
| UI 导出读回 | 三工作表，公式单元格为 0，前导零字符串保留 | `ui/run-05/export-readback.json` |
| UI 资源关闭 | 浏览器、服务、夹具线程全部结束 | `ui/run-05/service-state.json` |
| 数据库迁移 | 真实 v10→11 备份与历史保留；完整迁移后注入异常，DDL／数据／版本均回滚；旧 v4 路径通过 | `tests/test_multimodal_store.py`、后端日志 |

唯一跳过项为服务运行环境没有独立 GriTS 评分依赖的既有测试。错误输入、断网、取消、故障注入测试会有预期错误日志，最终结果以 unittest 汇总为准。

新增回归涵盖跨 Store 并发、同编号不同请求拒绝、模型配置删除后重放、仅校对限制、推理途中编辑／取消、同批目标重定位、跨任务过期、字符串与表格源码同步、不可改写原始结果、导出一致 SQLite 快照及损坏备用配置隔离。浏览器使用固定且明确标注的模型夹具验证业务流程，不能作为模型质量或速度证据。

## 真实本地模型实验

设备为本机 **RTX 4070 Ti SUPER 16GB**，不是目标 RTX A4000。记录的显存是 `nvidia-smi` 采样得到的整张设备占用峰值，包含桌面等基础占用约 2GB，不是模型独占分配。权重与视觉组件总计 3,413,361,504 字节，均完成完整 SHA256 校验。合成实验计时从队列执行开始，包含模型进程加载及退出，不含脚本先行执行的完整文件哈希；首次实际使用还需文件校验时间。

最终配置：`visual-review-v3`、单目标调用、16K context、最多 4096 输出 token、非 thinking、temperature 0、seed 0、单并发。本次没有针对文件名、表头、金额或已知答案分支。

| 实验 | 实际观察 | 用时／设备峰值 | 证据目录 |
|---|---|---|---|
| 合成中文文字与数字表格，可靠框 | 9 个目标：6 保留、3 修改；3 个人工植入的字符错误均改对，其余保持。脚本采用与撤销成功，原始 OCR 不变 | 12.54 秒／6285 MiB | `gpu-v3-crops` |
| 相同合成页，删除全部局部框 | 9 个目标：5 保留、4 修改；其中 1 项把标签“数量”串成邻格数字，属于错误建议 | 14.04 秒／6331 MiB | `gpu-v3-full-page` |
| 真实推理请求发出时取消 | 任务取消，0 条建议，编辑与原始结果不变，进程和 GPU 锁释放 | 取消至线程结束约 0.42 秒 | `gpu-v3-cancel` |
| 既有公开 Federal Register 页面 | 按通用规则选前 8 条非空正文目标，4 保留、2 存疑、2 修改；修改中出现跨栏目标被扩写为整段的范围问题 | 19.77 秒 | `gpu-public-v3/page-01` |
| 既有公开 NICS 表格页面 | 同规则选 8 条，5 保留、2 存疑、1 修改；数字串缩短的建议未被认定为有效纠错 | 14.96 秒 | `gpu-public-v3/page-02` |

以上目录均位于 `audit/multimodal-review-20260917`。两份公开材料复用此前开发材料，来源 URL 与哈希记录在 `public-inputs.json` 和实验回执中；模型观察共 9 保留、4 存疑、3 修改，**没有采用这些公开材料建议**。没有独立真值评分，不能把“修改数量”视作“纠错数量”。两页既有原图和结果哈希均保持不变。

合成材料专用于功能开发：明示的原始文本、人工植入错误和人工绑定格框都保存在脚本及回执里。脚本自动点选合成建议只是测试采用路径，不是产品自动采用策略。实际产品仍逐条由用户决定。

开发中保留了早期 `gpu-01`、`gpu-02`、`gpu-03` 与 `gpu-full-page` 记录：小模型会给出与文字矛盾的 keep/replace 标签，也会在多目标中串格。最终改为独立读图后由程序作确定性比较，并默认逐项调用。这是通用契约调整；既有失败没有被覆盖，也没有为样本答案增加修复规则。最终无框实验仍有错误，未继续围绕这一页调参。

## 交付与复现

新增完整应用目录：`D:/OCR-multimodal-workbench-20260917/bundle`，双击 `启动工作台.cmd`。既有 E 盘版本保持原样。新包包含既有四 OCR 引擎和新 4B 审校资源，约 29.5GB；3.6 大模型仅提供可选配置，资源不完整时明确不可用。

组包将旧包不可变资产复制并逐文件校验到新目录，再复制当前源码、配置和已构建前端，并生成新的完整清单。代码复制记录在 `bundle/locks/multimodal-copy-receipt.json`；完整包校验回执在交付目录外层的 `multimodal-bundle-verification.json`。安装目录内真实服务／GPU／HTTP／导出复验单独写入 `installed-smoke`，以实际回执为准。应用包不包含本次公开材料图像及评测工作区。

```powershell
$servicePython = 'E:/OCR-generic-tool-fixes-20260917/bundle/runtimes/service/python.exe'
& $servicePython -X utf8 -c "import sys,unittest;sys.path[:0]=['src','tests'];r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"

# 每次使用新的输出目录，原始失败与响应不覆盖。
& $servicePython -X utf8 scripts/audit_multimodal_runtime.py --bundle D:/OCR-multimodal-models-20260917 --output build/multimodal-reproduce/crops
& $servicePython -X utf8 scripts/audit_multimodal_runtime.py --bundle D:/OCR-multimodal-models-20260917 --output build/multimodal-reproduce/cancel --cancel-on-request
& $servicePython -X utf8 scripts/audit_multimodal_runtime.py --bundle D:/OCR-multimodal-models-20260917 --output build/multimodal-reproduce/full-page --no-local-geometry
& $servicePython -X utf8 scripts/audit_multimodal_ui.py --help
```

跨页结构仲裁、缺失版面／整表重建、自由模板 DOCX、主动取证和微调不属于本轮已实现范围。官方 9B、Qwen3.6 混合档、目标 A4000、另一台 Windows、系统禁网运行和独立质量集均没有本轮实际验证结论。
