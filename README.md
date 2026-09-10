# 纸页 · 内网离线 OCR 工作台 0.3.0

第二、三周版本已提供中文浏览器工作台、原生托盘启动器、项目与任务保存、四引擎顺序对比、图像校正、文字与表格校对和导出。第一周命令行验收工具仍随包保留。

## 运行便携包

解压完整包，双击 `启动工作台.cmd`，创建项目并导入照片。选择识别方式，完成后校对文字或表格，再点击「导出结果」。包内自带 CPython、四个独立运行环境、模型、CUDA 用户态 DLL、MSVC 运行库和浏览器静态资源。目标机只需要 Windows、Edge/Chrome 和兼容的 NVIDIA 驱动；不需要安装 Python、Node.js、CUDA Toolkit 或开发工具。

项目默认保存在 `%LOCALAPPDATA%\OfflineOCR\Workspace`，与应用目录分离。关闭浏览器不结束后台；从系统托盘重新打开或退出。异常退出后，未完成任务等待手动「继续」，已完成项不会重复。操作说明见 `docs/工作台使用说明.md`；本阶段验收结果见 `docs/第二三周审计报告.md`。

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

第一周历史验收范围和报告仍分别保留于 `docs/第一周验收范围.md`、`docs/第一周审计报告.md`。新版构建补充见 `docs/应用构建与复现.md`。
