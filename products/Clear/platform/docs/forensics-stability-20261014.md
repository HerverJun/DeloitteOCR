# 取证稳定性工程候选：使用、接口与回退

适用版本：Clear 0.11.1-rc1，经典工具箱 0.1.1。本轮修正坐标、分块接缝及复核查看；业务准确率与独立现场断网验收尚未完成，不输出真假概率或自动裁决。

## 使用

完整目录候选位于 `F:/OCR/output/forensics-stability-20261014/delivery/Clear-0.11.1-forensics-candidate-001`，包含模型与运行时。本机独立验证建议从候选目录执行 `启动审计平台.cmd -Port 8891`，从应用目录进入 ClearForensics；经典页为 `/apps/forensics`，CAT-Net 深度区域分析为 `/apps/forensics/deep`。先上传原件，再开始检查；CAT-Net 使用原始 JPEG。双击默认使用 8879，仅在该端口空闲时使用；正式入口登记不变。

新结果可切换原图、热图、叠加，调整透明度，并按适应窗口、1:1 或放大查看。坐标证明缺失、尺寸不符或图片加载失败时，叠加关闭，保留独立查看/下载。经典人工审阅仍记录判断与说明；CAT-Net 可在显示预览上框选，保存时反向转换至原存储坐标。

历史报告、热图、哈希、审阅版本与固定证据不重算、不替换。旧经典报告继续按旧版本独立查看；需要新版坐标结果时另建检查。旧 CAT-Net 框和原始概率数组仍按 JPEG 存储坐标解释，显示转换不会改写历史框。

## 接口与坐标

现有 API 路径与版本保留：

| 用途 | 路径 |
|---|---|
| 经典检查创建/列表 | `/api/platform/v1/workspaces/{workspace_id}/forensics` |
| 经典检查详情、审阅、下载 | 上述路径后接 `/{inspection_id}`、`/{inspection_id}/review`、`/{inspection_id}/download` |
| CAT-Net 任务创建/列表 | `/api/platform/v1/workspaces/{workspace_id}/multimodal/jobs`，创建请求 `kind=catnet` |
| CAT-Net 详情、复核、下载 | 上述路径后接 `/{job_id}`、`/{job_id}/review`、`/{job_id}/download` |

经典请求仍用 `schema_version=1.0`，六工具准入不变；报告 `schema_version=0.1` 保留，新增可选 `input.geometry`。新地图的 `metrics.map_coordinate_system=exif_transposed_pixels`；两个 JPEG 工具的分析坐标为 `stored_pixels`。JPEG 在存储像素分析后无插值映射热图；TIFF 使用编码 ImageWidth/ImageLength 确定存储尺寸，并沿用 ImageOps 解码归正，避免二次旋转。

CAT-Net 任务仍用既有 ManualAnalysis/ManualReview 1.0。`result.processing.geometry` 描述存储与显示坐标；`preview.png`、`heatmap.png`、`heatmap-probabilities.npy` 保持存储坐标；新增显示文件由 `processing.display_preview/display_heatmap` 指向 `preview-display.png`、`heatmap-display.png`。`regions.box` 保持相对存储宽高的归一化框，显示编辑必须通过逆变换后再保存。`result.width/height` 仍为存储尺寸，不能直接当显示尺寸。

共享几何契约为 `schema_version=1.0`、`coordinate_version=exif-pixel-edges-v1`；`stored_size/display_size` 为 `[宽,高]`，`stored_to_display/display_to_stored` 为互逆 3×3 矩阵。像素边界范围是 `[0,宽]×[0,高]`；离散像素中心为 `(x+0.5,y+0.5)`。缺失/非法 EXIF 按恒等处理，并分别记 absent/invalid，不猜测方向。

经典报告新增 `runtime.toolbox` 实际路径/版本，平台核验 `toolbox_source/toolbox_execution` 与当前派生清单一致。派生声明存在但不合法时拒绝执行，不回退到原始哈希。新成果绑定 0.1.1 与源码身份；已经 ready 的历史成果保持原样。

CAT-Net 的分块大小与 halo 是平台受控配置，不是新增的公开请求字段。`config/multimodal.json` 中本候选默认 halo=64，halo=0 保留为工程对照；GPU 仍需在实际 DATA 下通过本机资格，开发机/包内 CPU 收据不能代替。

Paddle 已知缓存入口通过 `manual_multimodal/paddle_cache.py` 在导入内存中定向到显式 F 盘 cache，仅改 `DATA_HOME/WEIGHTS_HOME` 的单赋值右侧；不改 HOME/USERPROFILE，不改 vendor 磁盘文件，不放宽盘外写 guard。第三方布局不匹配时拒绝适配；[真实 Paddle 3.3.1 导入审计](/F:/OCR/output/forensics-stability-20261014/evidence/paddle-cache-import-001/report.json) 与9项专项测试通过，C盘写入违规为零。最终CPU/GPU资格须绑定此修复后的源码，001旧回执不代替刷新。

## 验证边界

[CAT-Net 门槛](/F:/OCR/output/forensics-stability-20261014/evidence/catnet-comparison-001/acceptance-gate.json)：320 对合成输入与 12 组整图参考，接缝 MAE 降低 37.17%，像素 F1 下降 0.3732 个百分点，P95 为旧版 1.0151 倍，采样设备峰值 6577MiB，四门槛通过。该门槛衡量接缝/资源回归，不是业务准入。

[经典最终对照](/F:/OCR/output/forensics-stability-20261014/evidence/classical-comparison-002/report.json)：两版各 320 次真实六工具执行；新版两 JPEG 工具的存储坐标配准均为 0/320 失败。JPEG 重压缩合成改动图 AUPRC 下降，CAT-Net 冻结整图阈值在最终 32 张改动图上检出 0 张，均如实保留；不可据此自动判断真假。

## 源码权威与回退

本轮源码权威是 `codex/forensics-stability-r1` 隔离分支，路径 `F:/OCR/output/forensics-stability-20261014/worktrees/platform`。原开发仓保持基线 `eeea058fe4ac01716a89c0617e0815141888f426`，不合并、不改其 embedded runtime 路径或资格绑定。源码归档使用 `F:/OCR/scripts/sync_clear_sources.py` 的显式 `--platform-source` 指定本隔离源，由发布流程核对后同步；不是默认把旧开发仓当作新源码。

正式 `current` 未切换。若本候选验证失败，取消所属任务并等待实际进程/GPU清理，再停止候选平台，继续使用原正式入口。保留失败报告、候选和新 DATA，不删除锁、资格或证据强行继续。

正式升级前停止相关程序，冷备整个 DATA 到新的 F 盘目录并核对原件/复核/成果。回退使用旧程序和升级前备份，恢复至新的 F 盘目录；不让旧程序直接写入已升级 DATA，不原地覆盖、不重写历史热图或框。

完整交付/验收状态见 `F:/OCR/output/forensics-stability-20261014/DELIVERY-REPORT.md`。源码记录 ZIP 只供审阅，不含模型/运行时，不能替代完整目录离线启动。
