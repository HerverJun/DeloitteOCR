# 导入已有的 GLM-OCR 模型

GitHub 仓库只提供源码、配置和依赖锁，不提供模型权重或 Python 运行时。GLM-OCR 识别还需要 `PP-DocLayoutV3_safetensors` 布局模型，以及已按项目依赖锁准备好的 Windows GLM 独立运行时；仅有 GLM-OCR 权重目录不能执行识别。

如果已有完整的 OCR 工作台离线包或按 `docs/构建与复现.md` 准备的构建目录，可在本机把已有模型目录打成工作台能够导入的完整引擎包。命令不会下载模型，也不会改动原模型目录：

```powershell
python scripts/build_engine_package.py `
  --bundle "D:\DeloitteOCR\bundle" `
  --engine glm --id glm-local-v1 --version local-1 `
  --glm-model-dir "D:\Models\GLM-OCR" `
  --layout-model-dir "D:\Models\PP-DocLayoutV3_safetensors" `
  --output "D:\Packages\glm-local-v1.zip"
```

`--bundle` 指向包含 `app/`、`runtimes/glm/`、`config/` 和 `locks/glm.json` 的完整离线运行目录。如果其中已有布局模型，可以省略 `--layout-model-dir`。GLM 目录需含 `config.json`、`preprocessor_config.json`、`tokenizer.json` 或 `tokenizer.model`，以及权重文件；布局目录需含 `config.json` 和权重文件。模型目录中的 `.git`、`.cache`、`.huggingface` 元数据不会进入引擎包。

脚本生成 `glm-local-v1.zip` 和同名 `.sha256` 文件，按实际导入文件的内容哈希生成本地模型 revision 和清单。打开 OCR 工作台的「引擎管理」，选择该 ZIP，待完整性校验结束后启用。启用时工作台会运行依赖自检和真实识别；需要支持 BF16 的 NVIDIA CUDA GPU。新的包 ID 不能与已安装版本重复。导入失败时原启用版本保持不变。

该 ZIP **包含用户本地模型字节**，只应保存在用户自己的交付位置；不要提交到 GitHub。源码仓库的 `.gitignore` 排除模型权重、运行时和 ZIP。
