# ClearForensics 0.1.1 几何修正

此次变更验证的是热图像素位置与复核预览的对应关系，不声明篡改检出率提升，也不新增真假裁决。

## 分块与 JPEG 分析

噪声残差和 JPEG 重压缩统计仍以原图 32×32 像素块计算；右端及底端不足一块的部分仅使用实际像素，并将统计值回填到实际范围。旧版把 ceil 网格整体拉伸至原尺寸，非整块图像的边界会位移；新版不再拉伸。整块尺寸保留原来的统计行为。

JPEG 重压缩差异和 JPEG 网格使用文件解码后的存储像素计算，尚未进行 EXIF 旋转或翻转。计算完成后，热图使用与预览一致的 EXIF 像素排列；只有整数转置、旋转和翻转，不插值、不重采样。四个重压缩质量与网格统计公式没有扩展，不解释为完整压缩历史。其他经典工具继续分析 EXIF 归正图像。

## 可选几何契约

原有 `schema_version: 0.1`、文件名称、`input.width/height`、`input.coordinate_system` 与证据字段保留。新报告增加 `input.geometry`：

- `schema_version: 1.0`，`coordinate_version: exif-pixel-edges-v1`。
- `source_coordinate_system: stored_pixels`；`display_coordinate_system`、`map_coordinate_system` 和经典 `persisted_region_coordinate_system` 均为 `exif_transposed_pixels`。
- `stored_size` 和 `display_size` 为 `[宽, 高]`，显示尺寸与 `preview-exif-normalized.png` 及每张完成的热图完全一致。
- `source_exif_orientation` 保留原整数方向值；缺失或非法非整数值记 null。`exif_orientation` 是有效 1–8，缺失/非法时为 1；`orientation_status` 区分 valid、absent、invalid。
- `stored_to_display`、`display_to_stored` 是互逆的 3×3 齐次矩阵。坐标约定是像素边界，整图范围为 `[0, 宽] × [0, 高]`。离散像素的中心为 `(x + 0.5, y + 0.5)`，不能将右下边界替换为 `(宽 - 1, 高 - 1)`。

JPEG 证据的 `metrics.analysis_coordinate_system` 为 `stored_pixels`。所有完成热图增加 `metrics.map_coordinate_system` 和 `coordinate_version`；输出前检查热图与归正预览尺寸一致。图像与热图仍从左上角开始，不增加新的概率或融合分数。

## 历史兼容

已生成的 0.1.0 报告与热图保留其原始字节和哈希，不在读取、审阅、归集、下载或恢复时重算。旧报告没有 geometry 字段仍可查看及渲染离线 HTML。旧图不能通过显示时拉伸可靠修复；需要新坐标结果时，对原资料发起新的检查，保留旧检查及固定证据。已有经典人工审阅说明与版本合同不变。

原始源码来源哈希留在 source-manifest.json 的 files 中；当前代码哈希另存 derived.files，不能用当前哈希覆盖历史来源。

## 验证范围

`tests/unit/test_forensic_geometry.py` 覆盖 100×77、65×65 和单像素末块，非整块尺寸 EXIF 1–8 的精确像素置换与正逆矩阵，JPEG 两工具的编码坐标不变性、旧 Context 构造兼容、原文件字节/哈希不变、预览/热图尺寸、非 JPEG 跳过、旧离线报告及派生来源哈希。此测试不下载模型、不调用 GPU，也不替代真实业务正负例评测。


## TIFF 解码与隔离运行时

非 JPEG 沿用 Pillow/ImageOps 的归正流程。TIFF 解码器会在读取时应用方向并移除方向标签，不能在 convert("RGB") 后再次按旧标签旋转。TIFF 存储尺寸取 ImageWidth/ImageLength（256/257）编码标签，显示像素与此 EXIF 几何的尺寸必须一致；无法证明时拒绝产生配准声明。JPEG 仍保留未归正解码 RGB，两个压缩工具只分析该数组。

worker 在导入工具箱前将自身模块目录放到 sys.path 最前，覆盖 embedded ._pth 中的旧源码路径。新报告 runtime.toolbox 记录实际模块位置和版本；隔离子进程测试核准同目录模块与 0.1.1 版本。TIFF 1–8 覆盖 4×3 和 101×77、逐像素 ImageOps 对照及原件哈希不变。
