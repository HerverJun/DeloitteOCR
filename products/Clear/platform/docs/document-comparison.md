# 文档主题对比

入口：`/apps/comparisons`。选择项目后上传一份 PDF，以及一份 PPTX 或最多 100 张有序页面图片。可分别为两侧选择本地 OCR 或多模态 API；解析连接与主题比较连接独立保存。

## 使用流程

1. 在供应商设置填写协议、API URL、模型名与 Key。支持 OpenAI Chat Completions 兼容协议及 Anthropic Messages。图片解析连接需启用图片输入，并可运行独立测试；保存连接不执行工具调用探针。
2. 选择两侧材料和解析方式。本地默认 PaddleOCR-VL-1.6，也可选已安装的 MinerU Pro。多模态模式每次只发送一页图像和固定结构指令，页面之间不带聊天历史。
3. 选择项目进度模板，或填写自然语言要求生成字段草案，编辑字段、别名、容差与统计日期后保存。
4. 开始比较。页面结果与模型调用分别留存检查点；未知调用不会自动重发。
5. 打开逐页结果检查原图、文字、表格和来源。校对或指定页重跑会建立新修订。更改主题或比较模型时可使用“重新比较”，复用已解析页面。
6. 导出固定模板 HTML、比较 JSON、两侧 Markdown/JSON 与来源清单。成果中心同时保存固定版本，旧报告可继续下载。

本地 OCR 模式向比较模型发送解析文字和表格；多模态模式将选择的页面图发送到配置的解析端点。Key 使用当前 Windows 用户 DPAPI 加密，配置接口和报告不返回 Key。

## 数据与任务约定

- JSON 为比较的权威输入，Markdown 为阅读导出。页面保留原件哈希、真实页号、页图哈希、解析配置和模型身份。
- 多模态坐标仅表示模型估计；没有可靠坐标时使用页级来源。表格单元格不借用整表边界伪造定位。
- 不完整页面、抽取失败或证据无法验证均降低覆盖情况，报告保留信息不足。统计日期不同不会直接判为同口径矛盾。
- 结果分类固定为一致、存在差异、仅 A 提及、仅 B 提及、信息不足。模型不生成 HTML/CSS/JS。
- 同一结果快照与模板版本生成相同字节；HTML 自包含并适合打印，文本统一转义。
- PDF 上限 200 页，PPTX/图片上限 100 页，单文件上限 128 MiB。PPT 包含隐藏页并标记，排除备注，使用静态渲染。
- 本地 OCR 每批最多20页复用模型，每页验证后提交检查点；有其他GPU任务等待时在页面边界让出资源。多模态API仍严格一次一页。

## 接口

根路径：`/api/platform/v1/workspaces/{workspace_id}/comparisons`。

| 路径 | 方法 | 用途 |
|---|---|---|
| 根路径 | GET / POST | 历史列表 / 创建任务 |
| `/profiles` | GET / POST | 比较配置及版本 |
| `/profiles/suggest` | POST | 从自然语言生成字段草案 |
| `/connections` | GET / POST | 独立供应商连接及版本 |
| `/connections/{id}/probe` | POST | 结构化文本或单页图片能力测试 |
| `/{id}` | GET | 当前或指定 revision 的任务 |
| `/{id}/control/{cancel,resume}` | POST | 取消 / 继续 |
| `/{id}/rerun-pages` | POST | 指定侧和页码重跑，可切换引擎或连接 |
| `/{id}/recompare` | POST | 复用页面，以新的主题/模型重新比较 |
| `/{id}/pages/{side}/{number}` | GET | 固定修订的页面、文字、表格和诊断 |
| `/{id}/pages/{side}/{number}/image` | GET | 页图 |
| `/{id}/pages/{side}/{number}/review` | POST | 保留来源的人工文字/表格修订 |
| `/{id}/files/{name}` | GET | 固定成果下载 |

页/文件/任务 GET 接受 `revision`。所有写入继承平台本机 Origin 与项目边界。输入固定资产 ID、修订和 SHA256，任务创建有 request_id 幂等约束。

## 运行与维护

平台持久状态位于 DATA/comparisons，独立 SQLite schema v1；新增任务写入 `document-comparisons-v1` 读取能力标记。停止/启动后未完成调用按未知处理，已验证完成页可复用。不要删除兼容标记让旧程序打开新 DATA。

运行时只读复用已有 F 盘 Python、OCR 模型和运行库。LibreOffice 通过 `WORKBENCH_LIBREOFFICE` 固定路径，渲染 profile、临时文件及全部中间产物放入任务目录。发行候选分别固定自身源码、共享依赖及 LibreOffice 完整文件清单。

Office转换使用 `F:\OCR\temp\clear-layout\<本轮随机ID>` 的短暂存目录，避免Windows长路径造成转换失败；全部profile、应用缓存和临时文件也在该目录内。PDF和转换日志经哈希校验后归档到任务修订目录，成功后只清理本轮已核对归属的暂存目录；失败暂存留作排查。

## 验证边界

受控回归集 `tests/fixtures/comparison_regression_v1.json` 含 20 对、233 条事实和 132 个判断项，开发与封存各 10 对。该集使用模拟抽取/语义响应测试工程链路，不能将其精确率或召回率当作真实供应商模型质量。

真实本地版面解析、PDF/PPT 渲染、HTTP 接口、前端及固定模板分别留证。真实云模型质量需使用配置的供应商和经人工标注的业务样本独立验收。
