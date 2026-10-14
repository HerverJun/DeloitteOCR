# 外部 API 视觉审校

适用源码版本：0.12.0rc1，数据库 schema 12。

## 配置与使用

1. 打开已有采用结果，在「视觉审校」中点击「配置外部 API」。
2. 选择「OpenAI 兼容」或「Anthropic」，填写 API URL 和 Key。
3. 点击「获取模型」，在模型框搜索并选择；服务没有模型列表时，可以直接填写模型 ID。
4. 点击「测试并保存」。程序发送一张随机字符小测试图，检查鉴权、图像输入和审校 JSON；成功才替换原连接，并自动选中「外部 API · 模型名称」。
5. 点击「复核本页」，或在文字 / 表格中选定目标后「复核选中内容」。对照原图查看建议，再人工采用、拒绝或标记存疑。采用后可以撤销，原始 OCR 始终保留。

只保存一个外部连接。模型列表表示账号可见，是否支持视觉须以测试为准。编辑连接时，已保存的 Key 不回显，留空即可保留；修改 URL 或协议后须重新填写 Key。「清除连接」会删除配置及对应凭据，本地模型继续可选。

选中外部模型后，界面会显示服务地址。发起审校将发送页面上下文图片、带目标编号的局部裁剪、相应 OCR 文字和必要的目标上下文。没有可靠坐标的目标明确使用页面上下文，不猜测裁剪位置。取消会关闭当前 HTTP 等待、停止后续批次并丢弃迟到结果；服务端已收到的请求可能仍被供应商处理。

启动程序、打开项目、刷新审校模型目录及读取已保存连接均不访问供应商。只有主动获取模型、测试连接或提交 / 恢复外部审校任务时才访问所配置地址。断网时外部审校不可用，本地 OCR、校对与导出仍可使用；内网也可以配置可达的本地 API 服务。

## 地址和协议

| 输入示例 | 最终 API 前缀 |
| --- | --- |
| `https://api.example.com` | `https://api.example.com/v1` |
| `https://api.example.com/v1/` | `https://api.example.com/v1` |
| `http://10.0.0.8:8000/gateway/v1` | 保留自定义前缀 |
| `https://api.example.com/v1/chat/completions` | 去掉末尾接口名，保留 `/v1` |

自定义路径按填写的 API 前缀使用，例如 `/gateway` 会请求 `/gateway/models`。不在自定义前缀后猜测追加 `/v1`。地址不能包含用户名、密码、查询参数或锚点。

- **OpenAI 兼容**：Bearer Key，`GET /models`，`POST /chat/completions`；图片以 `image_url` 的 PNG Data URL 发送。优先 `max_completion_tokens`，仅在连接测试明确返回不支持时尝试 `max_tokens`，保存通过测试的方式。
- **Anthropic**：`x-api-key` 和 `anthropic-version: 2023-06-01`，`GET /models` 自动分页，`POST /messages`，使用 Base64 图片块。

接口相对于上述前缀。第一版不支持 Responses、Azure 专有鉴权、自定义请求头或代理配置。HTTP 和 HTTPS 均可，HTTPS 正常校验证书；不跟随重定向，也不读取环境代理设置。

获取模型总超时 15 秒；连接测试总超时 180 秒，审校每批请求超时 180 秒。生成请求不会自动重发，失败可手动重试。供应商返回截断、拒绝、工具调用、无效 JSON、目标缺失 / 重复 / 未知时，本次任务不产生可采用建议。

## 任务和凭据

外部审校由独立单任务网络队列执行，不占用本地 GPU 锁或卸载 OCR 模型。「仅校对模式」也可以审校已有采用结果。异常退出后，未完成任务等待用户恢复。

每个任务固定协议、地址、模型、配置版本、原图哈希及裁剪证据。修改或清除连接后，未发送批次和准备重试的旧任务必须重新提交；已发出的单批请求仍按旧快照记录结果。多批任务中途失败不会发布部分建议。内容变化后，旧任务不能覆盖新内容。

Key 加密文件位于 `%LOCALAPPDATA%\OfflineOCR\Credentials`，使用当前用户 Windows DPAPI。数据库只保存配置元数据与凭据引用，任务快照、API 返回、浏览器持久存储、日志、报告及应用交付包不保存 Key。迁移到另一台电脑或 Windows 用户后，重新填写 Key 即可。

数据库从 schema 11 自动升级到 12，旧审校记录归为本地执行；升级前沿用现有数据库备份机制。请勿用旧版程序直接打开已升级的数据库。

## 离线构建

已有的 0.11.0rc1 ZIP 尚未更新。新功能需要同步新版源码、前端构建与服务运行时依赖，不能只替换网页。

新增依赖为 `httpx==0.28.1`、`httpcore==1.0.9`、`certifi==2026.7.22`。版本及完整依赖见 `config/runtime-locks/service.txt`，wheel SHA256 见 `config/service-wheelhouse.json`，许可见 `licenses/external-review`。构建机联网准备一次 wheel，交付运行时不下载：

```powershell
python -m pip download --only-binary=:all: --no-deps --dest build/external-api-review/wheelhouse httpx==0.28.1 httpcore==1.0.9 certifi==2026.7.22
python scripts/prepare_external_review_runtime.py --runtime <新包目录>/runtimes/service --wheelhouse build/external-api-review/wheelhouse --bundle <新包目录>
```

第二条仅针对新的暂存包：逐项验 SHA256，再安装并检查依赖导入 / 版本，保存依赖回执。`scripts/prepare_multimodal_bundle.py` 的 `finalize` 阶段已接入此步骤，可通过 `--external-wheels` 指定离线 wheel 目录，最后重新生成完整文件清单。凭据目录位于应用包之外，不参与打包。

## 应用接口

以下路径使用工作台现有本机 Bearer 会话鉴权，Key 仅在 POST / PUT 表单请求中由浏览器发送给本机服务。

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/multimodal/external` | 脱敏配置、本机凭据是否可用；不联网探活 |
| `POST /api/multimodal/external/models` | 按当前表单拉取模型，不保存 |
| `PUT /api/multimodal/external` | 测试小图片，成功后保存 |
| `DELETE /api/multimodal/external` | 清除连接及凭据 |
| `POST /api/multimodal/external/queue/recover` | 恢复异常的网络队列工作线程 |
| `GET /api/multimodal/models` | 本地及外部审校选项 |

模型获取与保存请求字段：`protocol`（`openai` / `anthropic`）、`base_url`、`api_key`、`model`；获取列表可以省略模型。外部选项 ID 为 `external:<配置版本>`，原有审校提交 / 决策 / 撤销 / 导出接口保持兼容。

本轮验证使用本地模拟协议服务，覆盖图片传输、完整审校与人工采用。尚无用户真实凭据，未声称验证真实供应商的可用性或识别质量；实际连接还需完成一次测试图及一次真实文档局部审校。
