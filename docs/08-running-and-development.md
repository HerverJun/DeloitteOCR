# v0.1 运行与开发指南

## 本机启动

在项目根目录执行 `.\scripts\start.ps1`，打开 http://127.0.0.1:8765 。保持启动终端运行，按 Ctrl+C 停止 API 和本次创建的工作进程。脚本会检查端口占用，防止重复启动。

可使用 `.\scripts\start.ps1 -Port 8877` 更换端口，仍只监听 `127.0.0.1`。新端口的同源浏览器写入请求受支持。

不需要重新安装现有环境。系统 Anaconda 环境未被替换，项目采用 `.venv\Scripts\python.exe`。

## 在另一台 Windows 机器安装

前提：Windows x64、Python 3.12、Node.js 20.19+ 或 22.12+、pnpm 10.12.3。GPU 路径需要兼容 CUDA 12.8 的 NVIDIA 驱动。首次下载 PyTorch wheel 约 2.9 GB，还需预留环境、缓存、模型和业务数据空间。

```powershell
cd E:\project\image-classification-platform
.\scripts\setup.ps1 -Python python
.\scripts\start.ps1
```

脚本创建隔离环境，从 `requirements-windows-cu128.lock.txt` 安装已验证的完整依赖，使用 `pnpm-lock.yaml` 安装前端并构建。默认缓存位于 `E:\image-platform-data\caches`，无 E 盘时通过 `-CacheDir D:\image-platform-cache` 指定。

默认 Python 包源为清华镜像，PyTorch CUDA wheel 源为南京大学镜像，两个大 wheel 使用官方索引公布的 SHA256 校验；来源记录见 `sources/implementation-dependencies.json`。可以显式改用官方源：

```powershell
.\scripts\setup.ps1 -IndexUrl https://pypi.org/simple -TorchWheelBase https://download.pytorch.org/whl/cu128
```

下载器支持分块续传，只会在 SHA256 一致后生成可安装 wheel。PyTorch wheel 的 SHA 已固定；其余 Python 包锁定版本，尚未提供所有包的离线镜像或逐文件哈希锁。脚本在本机已有缓存和环境上验证过，尚未在第二台全新 Windows 主机完成安装验收。

首次训练会下载 Hugging Face 上固定 revision 的预训练权重，并校验固定 SHA256；需要网络。之后相同权重从缓存读取。无 GPU 时可以在训练/分类表单中选择 CPU；本脚本仍安装上述 GPU 版本依赖，CPU 精简安装不是本轮交付的验收范围。

## 图片、标签和版本

支持静态 JPEG / PNG / WebP / BMP。上传单文件限制 100 MiB，图像限制 8000 万像素。EXIF 方向先纠正，透明背景铺白，再保持比例缩放、留白至固定输入尺寸，执行统一归一化。

目录导入登记原文件的绝对路径和 SHA256，**不会复制整个目录**。移动、删除或修改原文件后，训练/分类会报告缺失或内容变化。浏览器上传的文件保存在平台数据目录。

类别目录示例：

```text
E:\business-images\
  invoice\图片1.jpg
  contract\图片2.png
  receipt\图片3.jpg
```

项目中创建 `invoice,发票`、`contract,合同`、`receipt,收据`，导入时启用目录类别识别。未匹配目录的图片保持未标注。

同一原文档的页、同一商品的多视角、同一原图的处理副本，应手动设置同一来源组。系统把同来源组和相同 SHA256 的图片合并为不可跨划分的连通分组；相同内容标签冲突时阻止发布。尚无感知近重复、OCR 或自动来源推断。

首次按组和类别进行约 60% / 20% / 20% 的训练/验证/测试划分。发布后，清单固定。之后的新版本保留已有划分，新独立分组进入训练集；与已有分组关联的新增图继承该组划分。更改/移除锁定的验证测试标签或连接不同历史划分会被阻止。若确需重新设计评估集，在新项目中明确建立新的评估基线。

## 训练与结果的含义

ResNet18 用于轻量基线，DINOv2 ViT-S/14 用于特征能力对照。两者冻结编码器，只训练线性头，特征按权重、预处理、输入尺寸、依赖版本和精度策略缓存。每次实验固定随机种子，按验证集选择分类头；测试集用于报告。

当前实现统一 FP32 并关闭 TF32，保证 PyTorch 与 ONNX 的验收可比较。导出完整编码器 + 归一化特征 + 分类头，动态 batch、固定空间尺寸、ONNX opset 17。

模型包包含模型、标签、阈值、清单、训练配置、依赖版本、评估、导出验证、许可证和哈希清单。ZIP 中的数据清单包含本机源文件路径和标签；不包含业务图片本身。用于其他机器时，单图推理只需要模型包和输入图片；继续训练还需要可用的原始训练数据。

默认所有预测进入“待复核”。开启试验性的自动接受会按每类验证集阈值决策，softmax 分数不等于实际正确率。阈值为 1 表示验证支持不足，禁用该类别自动接受，即使数值饱和为 1 仍待复核。人工确认写入标注修订，原任务中的预测和接受状态作为历史记录保留；结果卡会另显示当前人工标签。数据版本不会自动变化，需显式重新发布和训练。

独立分类命令示例：

```powershell
.\.venv\Scripts\python.exe -m image_platform.cli predict .local-data\models\model_fc64192b639d44cd E:\images\sample.png --device cuda --output result.json
```

模型 ID 是本次演示产物；新安装机器应替换成自己的模型包目录。命令不导入 PyTorch，但 GPU 运行仍需 CUDA/cuDNN DLL。当前由安装的 torch wheel 提供，其他部署可用 `ICP_CUDA_DLL_DIR` 指向兼容 DLL 目录；独立无 torch 的净环境部署尚未验收。

## 任务与故障恢复

后台工作进程串行执行任务，内部使用小批次和受限解码线程；全局租约避免重复占用 GPU。批量分类按批次提交结果，暂停/取消在批次边界生效。失败或取消任务可创建仅处理未完成项的新重试任务。

如果进程意外终止，启动后等待最长约 45 秒租约到期；分类任务自动继续，已提交结果保留。训练/目录导入中断后标记失败并重新创建，已导入资产仍保留。没有训练检查点续跑。

如果目录中的图片在登记后改变，恢复原文件可重新处理；若是有意替换图片，应重新导入以登记新内容身份，原任务的输入身份保持不变。

任务详情提供最后约 40 KB 日志，完整文件在数据目录 `logs/` 下。CUDA 初始化失败会报错，不会悄悄降为 CPU。显存不足时减小批大小后重试。

## 存储与备份

默认数据目录为项目内 `.local-data`，包含 SQLite、图片上传、缩略图、数据清单、模型、特征缓存和日志。可在启动前设置 `$env:ICP_DATA_DIR = 'E:\my-platform-data'`；API 与 worker 必须使用同一目录。设置后会打开对应目录中的独立平台数据。

停止平台后，备份整个数据目录以及通过目录导入的外部源图片。备份完成再启动。只复制 SQLite 文件不包含模型和图片；运行中仅复制数据库主文件可能遗漏 WAL 中的事务。迁移后外部图片绝对路径须保持可用。

## 本地开发

```powershell
# 终端 1
.\.venv\Scripts\python.exe -m image_platform.cli serve
# 终端 2
.\.venv\Scripts\python.exe -m image_platform.cli worker
# 终端 3
cd apps\web
pnpm dev
```

Vite 地址 http://127.0.0.1:5173 ，将 `/api` 代理到 8765。Python 改动后需重启相应进程，前端开发模式支持热更新；生产界面需重新 `pnpm build`。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m pip check
cd apps\web
pnpm build
$env:ICP_PYTHON = 'E:\project\image-classification-platform\.venv\Scripts\python.exe'
$env:PLAYWRIGHT_CHROMIUM_EXECUTABLE = 'C:\Users\A\AppData\Local\ms-playwright\chromium-1228\chrome-win64\chrome.exe'
pnpm test:e2e
```

浏览器验证使用外部 Chromium 并关闭 GPU 合成，需平台和合成演示项目已就绪；在其他机器上把浏览器路径替换成实际路径，或省略变量并运行 `pnpm exec playwright install chromium`。

`scripts/validate_pipeline.py` 是真实训练验证脚本，运行前先停止其他 worker，避免竞争领取任务。现有验证已完成，日常启动无需重复。`scripts/benchmark_ledger.py` 只验证 15 万条数据库记录的恢复与查询，不能代替真实图片吞吐验证。
