# 通用多模态审校：模型核验与离线运行配置

核查日期：2026-09-17。本页记录实际可核查的型号、固定产物和部署约束；参数名称、磁盘大小、实测显存是不同指标。新增模型只提出审校建议，不直接写回 OCR 原文。

## Qwen3.6 的 4B、9B 与 A3B

本次对官方 Hugging Face `author=Qwen&search=Qwen3.6` 的查询返回四个公开仓库：`Qwen3.6-27B`、`Qwen3.6-35B-A3B` 及各自的 FP8。此检索结果本身不能证明任何渠道都不存在其他版本，因此另查了官方 GitHub 系列 README 与官方模型卡：

- [官方系列 README](https://github.com/QwenLM/Qwen3.6) 的 News 明确列出：2026-03-02 发布 **Qwen3.5-9B、4B、2B、0.8B**；2026-04-16 发布 **Qwen3.6-35B-A3B**；2026-04-22 发布 **Qwen3.6-27B**。其 `Qwen3.6 Open Models` 区列出 27B 与 35B-A3B。
- [Qwen3.6-35B-A3B 固定模型卡](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md) 明确写 `35B in total and 3B activated`，含视觉编码器。HF safetensors 元数据总计 35,951,822,704 参数。
- [Qwen3.6-27B 固定模型卡](https://huggingface.co/Qwen/Qwen3.6-27B/blob/6a9e13bd6fc8f0983b9b99948120bc37f49c13e9/README.md) 是 dense 27B，含视觉编码器；HF 总计 27,781,427,952 参数。
- [Qwen3.5-4B 固定模型卡](https://huggingface.co/Qwen/Qwen3.5-4B/blob/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/README.md) 是 4B 级视觉语言模型；HF 总计 4,659,865,088 参数。官方 9B 版本也属于 Qwen3.5 系列。

所以当前证据支持“4B/9B 是 Qwen3.5 公开小型号，3.6 的 A3B 是 35B 总权重的稀疏激活量”。若后续提供另一官方型号或新发布链接，可增加配置 profile，不需要重写审校业务。这里没有将私有仓库、未公开权重或将来发布一概否定。

官网博客原始 HTML 是动态应用壳，本轮未把未取得的正文当作额外证据。HF 官方查询来源：[模型 API](https://huggingface.co/api/models?author=Qwen&search=Qwen3.6&limit=100)。

## 已锁定的两个部署 profile

| profile | 模型产物 | GGUF + F16 projector | 计算设置 | 当前用途 |
|---|---|---:|---|---|
| `qwen35-4b-q4` | Unsloth Qwen3.5-4B Q4_K_M | 2,740,937,888 + 672,423,616 字节，3.18 GiB | GPU layers=99，视觉 GPU | 16GB 设备的轻量候选与开发对照，配置默认 |
| `qwen36-35b-a3b-q4-hybrid` | Unsloth Qwen3.6-35B-A3B UD-Q4_K_M | 22,134,528,992 + 899,283,680 字节，21.45 GiB | 40 层中的 16 层 GPU，其余 CPU，视觉 GPU | 可选混合档，尚需单独实际验证；不能称 3B 小模型 |

两者基础模型许可为 Apache-2.0；GGUF 为社区转换，不能误称 Qwen 官方 GGUF。配置分别锁定转换仓库 revision `e87f176479d0855a907a41277aca2f8ee7a09523` 与 `a483e9e6cbd595906af30beda3187c2663a1118c`。权重、projector、基础模型许可文件都固定字节数与 SHA256。

产物来源：[3.5 GGUF 固定 revision](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/tree/e87f176479d0855a907a41277aca2f8ee7a09523)、[3.6 GGUF 固定 revision](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/tree/a483e9e6cbd595906af30beda3187c2663a1118c)。哈希来自 HF LFS OID，下载结束后按完整文件再次计算 SHA256。

3.6 混合档的 GPU 层数是保守实验起点，不代表显存、速度或质量已通过验收。21.45 GiB 文件体积超过 16 GiB 显存，不能全 GPU 装载本组合。CPU 卸载仍需足够主存和磁盘。不会在运行中自动切换量化、模型或层数来掩盖失败。

## 固定运行边界

复用已有 `llama.cpp b10894` Windows CUDA 运行时；配置列出当前 `llama-server.exe` 与全部 43 个 DLL 的完整 SHA256。运行时不访问 Hugging Face、不安装依赖、不下载模型。缺失或损坏资源会明确提示。

`load_config(bundle, profile_id=None, config_path=None)` 加载配置；`review_readiness(..., verify_hashes=False)` 供 UI 进行路径和大小预检，明确区分是否执行完整哈希。进入 `ReviewSession` 必须验证所有锁定资产。预检的“文件存在”不能视作已验证模型完整性。

文件哈希缓存仅在当前进程内存保存，绑定路径、文件 ID、大小、mtime、变更时间和预期 SHA256。Windows 特别读取 NTFS `FileBasicInfo.ChangeTime`，因为 Python 的 `st_ctime` 在 Windows 可能仍表示创建时间；即使篡改同长度文件并恢复 mtime 也会重验。没有可编辑的持久化“已校验”标记。路径必须处于 bundle 内；越界相对路径、外部链接和未锁定 DLL 均拒绝。profile、配置、基础模型 revision、模型/projector/runtime/prompt 哈希记录在结果 identity 中。

服务仅监听 `127.0.0.1` 随机端口，使用每次会话随机密钥；Python HTTP 禁用代理和重定向。Windows 后台进程隐藏窗口，用 kill-on-close Job Object 回收所有子进程，与已有 OCR 使用同一个 `%LOCALAPPDATA%/OfflineOCR/gpu.lock`。上层队列必须先卸载当前 OCR 引擎，防止 GPU 并发争用。取消检查贯穿校验、启动、HTTP 等待和批次边界；取消时退出 context 会关闭模型进程。原始 HTTP 请求中不保存鉴权密钥。

## 通用审校请求与失败处理

当前使用 `visual-review-v3` 固定通用提示词，不内置票据、报表或行业词典。图片和 OCR 内的文字全部作为待审查数据，不解释为工具或执行指令。提示词要求优先看目标裁图、逐字符比较；无裁图时仅在全页中能唯一定位目标才作明确判断。没有任意工具、代码执行、远程 URL 图片或模型自行访问文件的能力。

- 单任务最多 256 个目标，单目标 4000 字符，总计 40000 字符；默认每次调用仅审查 **1 个目标**，目标和定位上下文预算最多约 6000 字符。配置支持有界的小批实验，但当前默认不将多个格同时交给模型裁决，以降低串格风险。
- 每个目标附一张缩小的页面上下文；有 `text/cell/region/exact/span/line/verified` 级有效 polygon 才生成有 ID 的裁图。坐标越界、非有限值、缺框或弱定位使用整页，并明确缺少可靠裁图，不伪造单元格边界。`range_semantics` 继续传给模型作为定位说明。
- 单张目标图最多 1,048,576 像素，长边最多 1600；纯上下文图最多 262,144 像素。没有可靠框的目标共享较清晰的整页图。
- 固定 16K context、4096 最大输出 token、单并发、非 thinking、temperature=0、seed=0。这是可复现约束，不声称 GPU 推理跨机器逐 bit 一致。较长中文目标仍可能超过输出预算，截断明确失败；不静默裁掉原文或伪造保留结果。
- 模型输出由 JSON schema 限制为 `target_id / reading / reason`：reading 是完整视觉转录字符串，或明确弃权的 null。程序把字符串与 before 逐字比较，相同为 keep、不同为 replace；null 转为 uncertain 并保持 after=before。空字符串表示明确读取为空，和 null 不同。前导零、空格、标点都参与精确比较。模型无需同时产生容易矛盾的决定标签。
- 程序另验 ID 唯一和全覆盖、reading 的类型和长度、理由非纯空白；所有原始 reading 保存到每批 evidence，用户和存储 API 仍接收 `keep/replace/uncertain` 与完整 after。旧式矛盾标签不会被“修正后”接受。未知/重复/遗漏目标、额外字段、工具请求、JSON 错误、非 stop 结束、超长响应均不产生成功建议。
- 成功保存输入快照、每批 page/crop PNG、完整原始请求与 HTTP 响应、几何与图像 SHA256、模型身份和计时。模型判断不是独立真值，也不会因为 JSON 合法而自动采用。

## 资源准备与检查

开发期准备程序与运行服务分离。示例：

```powershell
python scripts/prepare_multimodal_models.py --profile qwen35-4b-q4 --output D:/OCR-multimodal-models-20260917/models
python scripts/prepare_multimodal_models.py --profile qwen35-4b-q4 --output D:/OCR-multimodal-models-20260917/models --verify-only
```

在需要代理的开发环境显式加 `--proxy http://127.0.0.1:7890`；运行服务不会使用该代理。准备程序支持 `.part` 与 HTTP Range 续传，校验返回范围、总大小和完整 SHA256 后才发布；已存在文件若与锁不一致会报错并保持原样。输出各模型独立子目录和带许可的 `source-manifest.json`，不改写其它 OCR 引擎。正式 bundle 还应带上相同配置和已锁定 llama 运行时。

本轮已在 `D:/OCR-multimodal-models-20260917/models/Qwen3.5-4B-GGUF` 准备完成 4B GGUF、F16 projector、LICENSE 与来源清单，三项资产均通过完整 SHA256。3.6 混合档只有配置与可续传的部分文件，目前不算已安装，也无该档实际推理结论。

本轮单元/集成测试包括真实本地 HTTP 的鉴权、禁止重定向、响应截断/畸形/超长、取消响应时间、目标覆盖/字面值守卫、路径与配置边界、完整哈希与 Windows 恢复 mtime 后的缓存失效、共享页面与定位裁图证据保存。它们验证工程协议，不替代模型准确率、显存峰值、延迟及引错率实测；GPU smoke 和质量结论应以同日期开发验收报告为准。
