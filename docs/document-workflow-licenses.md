# 文档工作流复用与许可

新增组件如下，原四引擎/CUDA/依赖许可证与 NOTICE 继续保留。licenses/document-workflow/receipts.json 保存上游 URL/ref/SHA256/取得状态；pdf-runtime 保存 51 wheel 的 METADATA 和许可证原文。原文优先于摘要。

| 来源 | 复用方式 | 许可与源码 |
|---|---|---|
| PaddleX v3.7.0 | table_recognition_v2 子管线及匹配，保留原始/派生框和模型版本 | Apache-2.0；upstream/paddlex/LICENSE 与固定 pipeline_v2.py |
| RapidTable 22592283c1f9d7c5a014c96c4ecc57de0b3ebfce | 研究对照，3.0.2 main.py 核同 SHA；非默认后端 | Apache-2.0；upstream/rapidtable |
| Docling 5ea6490ffdc57b2fd7de5cc436f2d0a22f2214d4 | 参考 PDF 区域选择/空间合并，工作台适配来源/冲突，不引入全模型管线 | MIT；upstream/docling 许可与参考代码 |
| Docling Parse 1fab490e4e59a39d70f375cd5092f4784036fa93 | 直接使用 7.19.1 逐页提取字符/内容 | MIT；upstream/docling-parse 与实际 wheel 许可 |
| OCRmyPDF ffee83231532f4f67b8c0e756cddec67446570c9 | 17.11.0 FPDF2 renderer、strip_invisible_text/CTM，不调用默认 OCR | MPL-2.0；upstream/ocrmypdf 有固定 _graft.py/许可。实际版本全部未修改 Python 源码在 runtimes/pdf/Lib/site-packages/ocrmypdf，wheel 在 wheelhouse/pdf，可取得相应 Source Code Form |
| Scribe OCR / Folio-OCR | 借鉴分页/原图高亮/切页保存交互 | 未复制或打包这些项目代码/资产 |

PDF 栈固定 docling-core 2.96.0、pypdfium2 5.13.0、pikepdf 10.13.0.post1、fpdf2 2.8.8 和全部传递依赖，见 config/runtime-locks/pdf.txt、pdf-wheelhouse.json。各组件自身 LICENSE/NOTICE 原样保留，不将所有依赖概括为单一许可。

5 个辅助模型来自 PaddlePaddle 官方：PP-LCNet_x1_0_table_cls、SLANeXt_wired/wireless、RT-DETR-L_wired/wireless_table_cell_det。config/table-model-lock.json 锁 repo/revision/文件 SHA；模型 README/config/source-manifest 随包保留，模型卡声明 Apache-2.0。使用已有 Paddle 环境，不改变四引擎依赖。

Noto Sans/SC/Symbols 2 来自 google/fonts commit 809e4d8b8d7e9364a914909bb777679606c178b8，OFL-1.1。fonts 保留原字体、静态 Regular 输出和 OFL；config/pdf-font-lock.json 锁来源/衍生哈希。可变字体以默认轴及 400 字重实例化，符号字体重存，构建脚本 prepare_pdf_fonts.py。字体随应用提供，不独立售卖，嵌入不改变文档自身许可。

PubTables-1M/FinTabNet/WTW 仅研究评测，图像、标注与含转录的模型 artifact 不随应用/源码打包。清单与统计保留。WTW 官方 License 是 CC BY-NC 4.0，不能被镜像 README 的 Apache 标签覆盖；upstream/wtw 有官方声明。Rapid 对照模型与研究 runtime 留本地研究目录。
