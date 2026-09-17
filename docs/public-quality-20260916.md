# 通用工具与公开材料复核 · 0.10.0rc2

本轮重点改善可恢复处理和复杂表格候选。遵循[通用工具约束](public-quality-tool-policy-20260916.md)：固定 pdfplumber 0.11.10 与上游默认参数，外围仅适配坐标、来源、采用和错误恢复。首次运行前的配置锁与最终配置逐字节相同；没有文件名、表头、金额或样本 ID 特判。自写边线原型已撤回，不进入交付包。

## 完成的行为

1. 区域 OCR 正常执行但未检出文字时，保存整页已有文字，并生成包含原图范围的待复核项。引擎异常、取消仍保留失败状态。未核对区域继续阻止 PDF 导出；修改保存内容会使确认失效。
2. 原生 PDF 解析同步生成 pdfplumber 候选，每个上游表格独立保留，包括重叠的备选。原生文字仍由既有可靠性检查与固定文字池提供，不使用表格工具输出的文字覆盖 OCR。用户显式接受，沿用原撤销重做与来源导出。
3. 建表只替换实际消耗的原生文字区间。穿插的 OCR 文字和未解决冲突完整保留，不因一段区域被转换为表格而丢失。
4. 已渲染页面重新处理时可以提取工具候选，无需替换原图和原生文字档案。处理缓存包含新的流程版本与工具配置标识。

默认继续为 Paddle + local-v2；schema 10，无自动结构采用或默认精确定位切换。扫描图、无线表格继续依赖已有 OCR/模型；没有声称 pdfplumber 能解决所有复杂表格。

## 验证结果

| 材料 / 检查 | 结果 | 含义与限制 |
|---|---|---|
| 原有公开材料 | 13/13 次页面运行保存结果；11 次待复核、2 次无内容冲突 | 10 份不同 PDF、12 张不同页面图；原 6 次空区域中断现在保留结果，26 个空区域仍需核对 |
| 新增上游公开回归文件 | 7/7 次保存结果，均待复核 | 7 份 PDF、6 个文档/模板组，包含一对原页与旋转变体；另有 9 个空区域，不作独立泛化成绩 |
| 未经脚本确认的 PDF 预检 | 原集合 2/13，新集合 0/7 就绪 | 继续阻止重叠冲突、空区域或缺少定位；处理完成不等于可直接交付 |
| 最终代码重放原有候选 | 5 页有可采用的工具候选，612 个 Excel 字符串逐格读回 | 采用是脚本动作，不是人工质量判断；所有原有冲突保留，撤销重做通过 |
| 合成契约验证 | 36 个旋转 / CropBox / DPI 组合通过 | 验证坐标与跨度适配；不作为真实材料质量成绩 |
| 后端 | 309 项运行，308 通过、1 跳过 | 官方 GriTS 依赖在独立评分环境，本服务运行时显式跳过 |
| 前端 | 32 项通过，TypeScript/Vite 构建通过 | 既有打包体积提示保留 |
| 外部 Edge | 结构复核 6 项、空区域复核 3 项通过 | 浏览器均关闭；脚本交互时间不作人工效率 |

具体可复核的结构变化：`column_span_1` 的工具候选保留两组跨 2 列表头和 4 个跨 2 行表头，同时把标题作为跨 8 列的独立首行，合计 51 行、395 个逻辑格。`row_span_2` 的主表候选为 7 行 × 10 列、66 个逻辑格，序号和 West Bengal 均跨 3 行。上游默认工具也产生额外的小表候选，并在缺少边线的材料上形成过大的合并格；这些不足没有按样本修补，也没有自动采用。

原有集合的推理期间，最终版只另改了版本号、几何来源枚举适配及建表的文字保留逻辑。OCR 执行和工具默认配置没有变化。两组候选均另在最终源码下重放，新增材料的完整处理使用最终源码。源代码差异回执、逐页失败/复核记录及来源哈希见 `audit/public-quality-20260916`；全部原始工作区与日志位于 `build/public-quality-20260916`。

**独立质量验收、真实人工效率仍未测。** 当前证据支持减少流程中断、增加可核查候选及保持数据完整性；不能据此宣布准确率或泛化能力达到某一门槛。

## 使用与复现

完整交付位于 `E:\OCR-public-quality-20260916`，运行 `bundle/启动工作台.cmd`。原两个交付目录保持不变。PDF 处理后，在“结构复核”查看“PDF 表格工具”候选，对照原图选择；“页面内容待核对”中的空区域可以跳转原图。检查无文字遗漏后显式确认，有文字则补录并定位或重新 OCR。

工具代码使用 MIT 许可，原文见 `licenses/pdfplumber/LICENSE.txt`。完整包内包含固定工具文件，运行时不下载。它复用已有隔离 PDF 运行时：pdfminer.six 20260107、Pillow 12.3.0、pypdfium2 5.13.0。既有依赖及其许可不变，旧 GLM 运行时的 PyMuPDF 许可说明仍适用。公开 PDF 与生成的真实材料导出留在外部证据目录，不随应用 ZIP 分发。

```powershell
$bundlePath = 'E:/OCR-public-quality-20260916/bundle'
$servicePython = "$bundlePath/runtimes/service/python.exe"
& $servicePython -X utf8 -c "import sys,unittest;sys.path[:0]=['src','tests'];r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"
# 新目录输出；prepare 为联网材料准备步骤，产品处理仍离线。
& $servicePython -X utf8 scripts/audit_public_quality.py --phase prepare --output build/public-quality-reproduce/data --bundle $bundlePath
& $servicePython -X utf8 scripts/audit_public_quality.py --phase product --data build/public-quality-reproduce/data --output build/public-quality-reproduce/product --bundle $bundlePath
& $servicePython -X utf8 scripts/audit_public_quality.py --phase review --data build/public-quality-reproduce/product --output build/public-quality-reproduce/review --bundle $bundlePath
```

源码环境需要按 `config/pdf-table-tool.json` 放置锁定工具文件，可从完整包复制其同名 `build/public-quality-20260916/tool/site-packages` 目录。产品与源码包使用同一冻结锁。最终安装副本检查、完整包哈希和启动回执以交付目录实际文件为准。
