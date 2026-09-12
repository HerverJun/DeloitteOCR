# 公开真实图片样本 — 2026-09-12

最终选用 6 张公开真实来源图片。每张均已人工视觉检查，没有 OCR 检测框或转写叠加；没有用合成 fixtures 冒充真实样本。用途是工作台用户流程实测，不是严谨 OCR 准确率认证。

samples 目录仅有这 6 张图片，可以整体导入。来源证据及未采用候选放在独立的 sample-provenance 目录。

| 文件 | 尺寸 / 实际格式 | 内容 | 来源与许可/限制 |
|---|---|---|---|
| [chinese_store.jpg](samples/chinese_store.jpg) | 640×339 / JPEG | 上海愚园路蓝色路牌实拍，中英文字，背景建筑与透视。 | [固定来源](https://raw.githubusercontent.com/JaidedAI/EasyOCR/363afb184047ce452e436f4224f3098422df872e/examples/chinese.jpg)；Repository LICENSE is Apache-2.0; image-specific third-party rights were not independently established. Keep this audit copy for local evaluation; this manifest does not grant redistribution/commercial rights to the underlying document. |
| [english_book_photo.jpg](samples/english_book_photo.jpg) | 1100×708 / JPEG | 真实打开书籍的两页照片，弯曲书脊、阴影、漫画和数学式，适合去弯曲/裁剪/布局测试。 | [固定来源](https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/tests/test_files/book.jpg)；Repository LICENSE is Apache-2.0; image-specific third-party rights were not independently established. Keep this audit copy for local evaluation; this manifest does not grant redistribution/commercial rights to the underlying document. |
| [pubmed_table.png](samples/pubmed_table.png) | 503×98 / PNG | PubTabNet/PMC524509 真实科学论文表格，503×98 原始低分辨率，包含分组表头、四条数据行和 ± 数值。 | [固定来源](https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/docs/datasets/images/table_PubTabNet_demo/PMC524509_007_00.png)；PubTabNet requires CDLA-Permissive-1.0 per the linked PaddleOCR table dataset documentation; underlying publication attribution should be retained. Source: https://github.com/ibm-aur-nlp/PubTabNet ; license: https://cdla.io/permissive-1-0/ . |
| [document_formula.png](samples/document_formula.png) | 816×1056 / PNG | 英文学术论文第 3 页，双栏、公式 (4)–(14)、上下标及 PDF 原生彩色引用框；彩色引用框属于文档本身，不是 OCR 结果。 | [固定来源](https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/tests/test_files/doc_with_formula.png)；Repository LICENSE is Apache-2.0; image-specific third-party rights were not independently established. Keep this audit copy for local evaluation; this manifest does not grant redistribution/commercial rights to the underlying document. |
| [chinese_scanned_exam.png](samples/chinese_scanned_exam.png) | 1654×2339 / JPEG | 中文诗词练习整页扫描，有浅绿原生水印、填空横线和选择题。 | [固定来源](https://hf-mirror.com/datasets/opendatalab/OmniDocBench/resolve/aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec/images/jiaocai_jiaocai_en_6.jpg)；OmniDocBench copyright statement: research purposes only, not for commercial use. Do not include these images in the product release. Copyright belongs to source owners. |
| [chinese_handwriting.png](samples/chinese_handwriting.png) | 1434×2024 / JPEG | 真实中文地理手写笔记整页，荧光笔强调、流程箭头及底部手绘空气质量表格。 | [固定来源](https://hf-mirror.com/datasets/opendatalab/OmniDocBench/resolve/aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec/images/notes_1ba14cb325bc448f7201b20502ecf2b5_15.jpg)；OmniDocBench copyright statement: research purposes only, not for commercial use. Do not include these images in the product release. Copyright belongs to source owners. |

两张 OmniDocBench 图片复用 E:/OCR-final-build/public-benchmark 的完整原始页面缓存，并核对 benchmark.json 的 source_image_sha256。它们不是区域裁剪；缓存扩展名为 .png，但文件实际为 JPEG，保留字节与原始命名以便追溯。

PubTabNet 使用限制依据已下载的 PaddleOCR-table-datasets.md；PaddleOCR、EasyOCR 仓库的 Apache-2.0 LICENSE 已保留。仓库许可证不能被自动解释为原论文/书籍所有第三方内容均可商业再分发。OmniDocBench 明确限研究、禁商业用途，本轮素材不应打包进产品发布。

## SHA-256

| 文件 | SHA-256 |
|---|---|
| chinese_store.jpg | `7d8f231016c6a35da879b7d79b39c45b694aef962ae30f64063d981e618fbf7f` |
| english_book_photo.jpg | `782cc71a2730020ea8da815de3e40a964b7f150c183d1f8c01dd211de9dcbcc4` |
| pubmed_table.png | `8ac6eb772c45be3f17f28ce565489b5895ab47002fdf5b34590d57f047041130` |
| document_formula.png | `6b07d28527dc9e930804fa73df562f1a81599c6b8a1a8bbc2a80742fa9f26e80` |
| chinese_scanned_exam.png | `85da27e8868dbe1bff597eafc7e11effaaca6e432206a6fa09d78da35f1fbeb1` |
| chinese_handwriting.png | `22e5a99ac6a87a417a059922f4141dac2acebaa115f7b28e826c49a9950796f4` |

## 检查内容

sample-sources.json 包含来源 revision、URL、缓存路径、图片尺寸、实际格式、SHA-256、预期文字锚点及表格目视参考值。参考值用于抽查，未宣称独立裁决的完整 ground truth。

已排除：带检测框的车辆铭牌/登机牌、WildReceipt 文档注释图、人工混合字符串表格、插画式笔记。WHO 英文海报为真实公开海报，但为控制样本数未纳入最终六张。
