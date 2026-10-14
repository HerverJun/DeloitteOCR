# 环境与依赖准备

## 本机检查结果

检查日期 2026-09-18，实际环境与原调研列出的目标设备不同。

| 项目 | 检查结果 | 使用建议 |
| --- | --- | --- |
| 系统 | Windows 11 企业版 64 位，10.0.22631 | 首版可原生 Windows 开发 |
| CPU | Intel i5-13600KF，14 核 20 逻辑处理器 | 逐步测试解码并发，不直接占满全部线程 |
| 内存 | 约 31.8 GiB | 采用有界队列、分页与分片缓存 |
| GPU | RTX 4070 Ti SUPER，16376 MiB | 硬件可识别，当前 Python 运行时仅支持 CPU |
| NVIDIA 驱动 | 595.97 | 驱动存在不等于 torch/ORT CUDA 包可用 |
| C 盘可用 | 最新报告约 11.6 GiB，首次约 14.0 GiB | 空间会变化，不作为数据、模型和依赖下载默认盘 |
| E 盘可用 | 最新报告约 367.9 GiB，首次约 395.7 GiB | 空间会变化，创建环境和批量任务前重新检查 |
| 当前 Python | Anaconda Python 3.12.7 | 保留既有环境，项目另建隔离环境 |
| 当前 PyTorch | 2.9.1+cpu，CUDA 不可用 | 必须为项目另装 GPU 版本 |
| 当前 ONNX Runtime | 1.26.0，Azure 与 CPU provider | 未安装 onnxruntime-gpu，不能执行 GPU 推理 |
| 当前 timm | 1.0.22 | 两个候选名称可在注册表找到，但尚未加载权重 |
| Node | 22.17.0 | 可作为前端候选，依照 Vite 版本检查支持范围 |
| pnpm | 10.12.3 | 保存 packageManager 与 pnpm-lock.yaml |
| Git | 2.48.1.windows.1 | 可用，尚未创建项目远程仓库 |
| Docker | 可执行文件存在 | 未检查 daemon、WSL 或 GPU 透传；首版不依赖它 |
| uv | 当前 PATH 未找到 | 可选环境管理器，不是前置要求 |

可运行 `tools/preflight.py` 保存新的机器报告。默认只读取信息，不安装依赖、启动 Docker 或修改环境。本次已运行 `--probe-ml`，确认当前解释器中的 torch 和 ORT 仅有 CPU 能力；这仍不等于已完成真实模型推理验收。报告保存在 `reports/environment.json`。

## 依赖分组

| 分组 | 依赖候选 | 验证重点 |
| --- | --- | --- |
| core | Pillow NumPy Pydantic | 解码、数值类型、契约版本 |
| train | torch torchvision timm scikit-learn | GPU 可用、冻结特征、评估工具；不强制用 sklearn 训练头 |
| export | onnx onnxscript 及导出器要求的包 | opset、动态 batch、图检查与一致性 |
| runtime-gpu | onnxruntime-gpu 及匹配 CUDA/cuDNN | provider 实际执行、独立推理环境 |
| runtime-cpu | onnxruntime | 与 GPU 环境分开，不在同一环境同时安装两种发行包 |
| api | FastAPI Uvicorn SQLAlchemy Alembic | 数据迁移、进程边界、API 类型 |
| dev | pytest ruff mypy 或 pyright | 必要检查，不绑定开发机全局包 |
| frontend | React TypeScript Vite Ant Design TanStack Query | Node engines、锁文件与构建 |

当前没有生产 requirements 锁文件。timm v1.0.29 已通过官方发布接口核验；其他版本需要 P0 联合验证后冻结。调研文档中的仓库提交不能替代依赖解析和 wheel 兼容性验证。

## GPU 版本选择

ONNX Runtime 官方 CUDA 页面在本次访问时写明：从 1.27 起，PyPI 的 GPU 包默认 CUDA 13.0；CUDA 与 cuDNN 的主版本都要与实际运行依赖匹配。cuDNN 8 和 9 不可互换。这意味着不能复制一条不带版本的安装命令，并假定它永远是 CUDA 12 环境。

P0 先选择一套支持 Python 3.12、Windows 和目标显卡的已发布组合，记录 PyTorch wheel 来源、torchvision 配套版本、ORT 包来源、CUDA/cuDNN 运行库。若采用 CUDA 12 路径，显式选择兼容发行包；若采用 CUDA 13 路径，先核对所有目标机器驱动与依赖。nvidia-smi 显示的 CUDA 支持上限不等于已安装 toolkit 版本。

运行最小 torch CUDA 张量运算与真实 ONNX GPU 前向，检查 session 实际 provider 和运行日志，避免“列出 CUDAExecutionProvider”但实际节点回退 CPU。GPU 必需模式下不可静默转 CPU。推理环境若不含 PyTorch，必须独立提供 DLL/运行库，不能依赖训练环境恰好已 import torch。

## 推荐安装顺序

1. 运行预检，确认 E 盘配额；创建项目隔离环境，建议 Python 3.12。
2. 从官方匹配索引安装所选 torch/torchvision，验证 CUDA 张量运算。
3. 安装固定 timm 与导出依赖，加载两种编码器并登记权重。
4. 在独立推理环境安装匹配 ORT GPU 和运行库，完成真实 ONNX 前向。
5. 保存准确版本、安装索引、wheel 来源、权重哈希与许可证；使用锁文件在干净环境重建。
6. 再安装 API 和前端依赖，实现 P1/P3。

准备阶段不改动 Anaconda base，也不批量下载模型。P0 执行时才确定并安装 GPU 大包，避免在未选定兼容组合前占用磁盘。

## 存储与运行配置

建议将 HF_HOME、TORCH_HOME、PIP_CACHE_DIR 和项目临时目录设置到 E:/image-platform-data/caches 下，只在项目启动脚本的进程作用域设置；不修改用户全局环境。模型包保留下载出处与文件哈希，即使缓存清理也能重新构建。

存储估算应实测平均原图字节数：若平均 1 MB，15 万张原图约 150 GB；全部复制归档另需约 150 GB，再加缩略图、训练样本、缓存、环境和备份。这个例子不是已测数据大小。先抽样并检查空间，引用已有图片通常可以减少重复存储。

首版 CPU 模式可做接口和少量推理诊断，不承诺完成 15 万张的目标时长。A4000、A6000 属于需要单独预检和压测的目标机器，当前没有连接或验证这两台机器。

## 交付环境产物

P0 结束时应具备训练环境锁、GPU 推理环境锁、CPU 诊断环境锁、安装来源记录、GPU 与驱动报告、模型哈希、ONNX 验证报告。P3 增加 pnpm 锁与前端构建记录；P4 增加启动、备份、升级和回退操作说明。
