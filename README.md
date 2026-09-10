# 内网离线 OCR 工作台 · 第一周

本阶段交付四个 Windows 原生 OCR 后端的真实兼容性验证包和审计工具。托盘、浏览器 GUI、持久化队列和编辑工作流按《开发总纲》在后续阶段实现。

## 运行便携包

解压完整包，双击 `运行兼容性验收.cmd`。包内自带 CPython、四个独立运行环境、模型、CUDA 用户态 DLL 和 MSVC 运行库。目标机只需要 Windows 和兼容的 NVIDIA 驱动；不需要安装 Python、Node.js、CUDA Toolkit 或开发工具。

也可以在包目录打开终端：

```powershell
.\ocr.cmd doctor
.\ocr.cmd verify
.\ocr.cmd recognize --engine all --image "D:\照片\测试图片.png" --output "D:\识别结果\本次运行"
```

引擎参数为 `ppocr`、`paddlevl`、`glm`、`hunyuan` 或 `all`。`all` 顺序启动独立进程，每个引擎结束即释放进程资源。输出目录必须为空，防止旧结果被误当作本次成功证据。

输出包括原始模型 JSON、统一 JSON、TXT、Markdown、对应坐标的 `input.png` 和可用时的 XLSX。原图不会被修改。模型缺失的置信度或坐标为 `null`。PP-OCRv6 不具备表格结构识别能力，表格照片仍返回文字块，不能据此冒充 Excel 结构还原。

## 源码验证与构建

普通测试不加载模型：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```

构建流程、精确版本、Paddle Windows cuDNN 依赖修正及离线验收方法见 `docs/构建与复现.md`。所有安装发生在构建目录的嵌入式 Python 运行时，不能直接复制开发机虚拟环境作为交付物。

验收范围见 `docs/第一周验收范围.md`；最终结论与证据索引见 `docs/第一周审计报告.md`。
