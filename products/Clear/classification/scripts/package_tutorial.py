"""Produce a concise, evidence-linked user guide for the recorded tutorial."""
import json
from pathlib import Path
import shutil
import zipfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'deliverables/花卉三分类教学'
WORK = ROOT/'reports/tutorial'
report = json.loads((WORK/'recording/report.json').read_text(encoding='utf-8'))
assert report['status'] == 'passed'
timeline = report['timeline']
chapters = {c['id']: c for c in json.loads((WORK/'narration.json').read_text(encoding='utf-8'))}
source = Path('E:/image-platform-data/tutorial-flowers')
manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
OUT.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(OUT/'公开教学数据_花卉三分类.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for row in manifest['records']:
        file = Path(row['path'])
        archive.write(file, Path('tutorial-flowers')/file.relative_to(source))
    for name in ['manifest.json','README.txt']:
        archive.write(source/name, Path('tutorial-flowers')/name)
with zipfile.ZipFile(OUT/'公开教学数据_花卉三分类.zip') as archive:
    assert archive.testzip() is None
shutil.copy2(ROOT/'sources/flowers102-provenance.json', OUT/'公开数据来源与校验.json')
toc='\n'.join(f"| {int(s['start'])//60:02d}:{int(s['start'])%60:02d} | {chapters[s['id']]['title'][5:]} |" for s in timeline)
with urlopen('http://127.0.0.1:8765/api/v1/projects/'+report['project_id']) as response:
    project_name = json.load(response)['name']
readme=f'''# Prism 从零开始分类：花卉三分类

直接播放同目录的 `Prism_从零开始图像分类教学.mp4`。视频为真实浏览器操作录屏，配有离线合成中文语音、烧录字幕、点击提示及 17 个可跳转章节；没有替换分类结果，也没有加速隐藏训练等待。

- 平台地址：http://127.0.0.1:8765/
- 已保留项目：**{project_name}**（从左侧“当前项目”切换）
- 项目 ID：`{report['project_id']}`
- 当前模型：`{report['model']['id']}`
- 当前分类任务：`{report['inference_job_id']}`

## 文件说明

| 文件 | 用途 |
| --- | --- |
| Prism_从零开始图像分类教学.mp4 | 主教学视频，含语音与字幕 |
| 中文字幕.srt | 单独字幕文件 |
| 公开教学数据_花卉三分类.zip | 90 张公开真实照片和来源/标签清单 |
| 花卉分类模型.zip | 本次真实训练、验证并下载的 ONNX 模型包 |
| 分类结果.csv / 分类结果.jsonl | 30 张独立待分类照片的结果 |
| 全流程验收记录.json | 录屏运行的任务、模型、指标和断言结果 |
| 测试验收说明.md | 测试范围、实测结果和导出字段说明 |
| 独立待分类样本预览.jpg | 30 张照片索引，显示核验用真实类别 |

## 跟着视频重新操作

本教学从空分类项目开始；软件环境和服务已安装启动，预训练编码器权重在本机已有缓存。其他电脑第一次训练可能下载权重。平台停止时，在项目目录运行 `powershell -ExecutionPolicy Bypass -File scripts/start.ps1`；该窗口须保持运行。

1. 解压数据包。本机已经有 `E:\\image-platform-data\\tutorial-flowers`，可直接使用。
2. 新建项目，类别定义每行一个：

```text
daisy,牛眼菊
rose,玫瑰
sunflower,向日葵
```

3. 在“图像与标注”导入 `E:\\image-platform-data\\tutorial-flowers\\01-prepare`，保留按子目录匹配类别。得到 59 张已标注和 1 张未标注图片。
4. 筛选“未标注”，勾选唯一的一张玫瑰，设置类别“玫瑰”，点击“保存标注”。三类各 20 张。
5. “训练与实验”→“发布数据版本”，种子 42。冻结 60 张：36 训练、12 验证、12 测试。正式业务中，同一对象/文档/视频的不同图片应使用相同来源分组。
6. 编码器选择 DINOv2，分类头选择逻辑回归，分辨率 224，计算设备 NVIDIA GPU。点击“开始训练并导出 ONNX”。本操作训练新分类头，编码器保持冻结；不等于从随机权重训练整个视觉模型。
7. 任务完成后打开“模型库”的“评估详情”。查看内部测试指标、混淆矩阵，再下载模型包。
8. 导入 `E:\\image-platform-data\\tutorial-flowers\\02-classify`。这 30 张图片不含类别目录，保持未标注；不要把清单里的真实类别导入平台。
9. “分类与复核”→“新建分类任务”，范围选“未标注图片”，选择刚训练的模型。保持自动接受关闭，点击“开始分类”。
10. 任务完成后点击“详情”，逐张查看预测。点击“查看并确认”，检查原图、选择真实类别，再“确认并保存”。
11. 用 CSV / JSONL 按钮导出全部 30 条结果。刷新页面后任务与人工标签仍保留。

## 视频章节

| 时间 | 内容 |
| --- | --- |
{toc}

## 数据来源与独立性

原始公开数据为 [Oxford Flowers102](https://www.robots.ox.ac.uk/~vgg/data/flowers/102/)，共 102 类。本教学只使用其中 **49 牛眼菊、74 玫瑰、54 向日葵**三类。

60 张准备样本来自官方训练和验证集合；30 张待分类图片来自官方测试集合。按图像编号确定性抽取，没有根据本次模型成绩筛选样本。90 张照片的 SHA256 均不同；63 位感知哈希的最小距离为 {manifest['minimum_phash_distance']}，没有距离 ≤6 的近重复候选。哈希筛查并不等于对所有拍摄场景做了人工来源审计。

清单保存官方类别、官方划分、图像编号和 SHA256。独立图片是在模型训练完成后才导入新项目，没有用于本项目的训练、验证参数选择或阈值选择。内部 12 张测试图与额外 30 张分类图是两个不同集合。

图片权利归原作者，本地教学副本保留来源信息；其他用途应核对原始数据使用条件。
'''
(OUT/'使用说明.md').write_text(readme,encoding='utf-8-sig')
tests=f'''# 全流程验收说明

验收日期：{report['started_at']}。录屏项目：{project_name}。

## 实测结果

- 新项目完整操作流程通过，录屏运行包含 {len(report['checks'])} 项业务断言，浏览器脚本错误 {len(report['page_errors'])} 个、API HTTP 错误 {len(report['http_errors'])} 个。
- 独立待分类照片：{report['holdout']['correct']}/{report['holdout']['count']} 张预测正确；3 类各 10 张。该 30 张教学子集很小，不能据此声称所有类别或业务场景都是 100% 准确。
- 模型内部测试集：{report['model']['metrics']['test']['sample_count']} 张，准确率 {report['model']['metrics']['test']['accuracy']:.1%}；验证集 {report['model']['metrics']['validation']['sample_count']} 张。
- 本次分类任务完成 {report['inference_job']['done']} 张，失败 {report['inference_job']['failed']} 张。
- 所有预测保持“待复核”。视频示范确认 1 张，不代表其余 29 张已经人工审核。
- 后端回归测试 33 项通过；浏览器常规回归测试 6 项通过。独立新项目验收另行运行，覆盖真实训练和独立照片推理。

## 验收覆盖

新建空项目 → 目录导入 → 自动/人工标注 → 固定数据划分 → DINOv2 冻结特征 + 逻辑回归分类头训练 → ONNX 导出与数值校验 → 模型下载 → 独立图片导入 → GPU 批量分类 → 人工复核 → CSV / JSONL 导出 → 浏览器刷新后数据持续保存。

另有常规测试覆盖上传、无效输入、项目隔离、移动端导航、暂停/取消任务、模型评估展示等。验收不代表已覆盖任意文件格式、超大数据集、断电或所有硬件组合。

## 本轮修复：导出保留人工标注

此前导出只有原始预测，无法携带人工复核结果。现在增加两个字段，保持原有字段兼容：

| 字段 | 含义 |
| --- | --- |
| label_id | 本次任务当时的模型预测，不随人工改标签而改变 |
| current_annotation_label_id | 导出时该图片的当前标注；未标注为空。它可能来自人工确认，也可能来自原先的导入标注 |
| effective_label_id | 有当前标注时优先用当前标注，否则使用原始预测 |
| status | 原始预测的接受状态；人工确认后仍可为 needs_review，用于保留原始决策记录 |

需要“仅人工/已有标注”的数据时筛选非空 `current_annotation_label_id`。不要仅凭 `effective_label_id` 非空就认定已人工确认。原始预测字段为固定记录；当前标注字段反映每次导出时的最新标注，因此重新导出可能随后续修改而变化。

回归测试分别验证 CSV 与 JSONL：无标注时回退预测、把类别 A 更正为 B 后同时保留 A/B、清除标注后回退预测，以及原始预测和状态完全不变。

## 可复现入口

- `scripts/prepare_tutorial_data.py`：从已校验的官方下载缓存生成教学子集。
- `apps/web/scripts/tutorial-flow.mjs`：通过真实界面执行完整新项目验收；API 只用于只读核验和等待。
- `scripts/prepare_tutorial_narration.py --synthesize`：生成离线中文配音与时间信息。
- 在 `apps/web` 运行 `node scripts/tutorial-flow.mjs --record`：重新录屏并再次执行验收，会新建独立项目。
- `scripts/render_tutorial.py`：合成视频、字幕和章节。
- `scripts/package_tutorial.py`：整理数据包、使用说明和测试报告。

完整任务 ID、数据快照、模型指标、逐图核验、操作时间线见 `全流程验收记录.json`。源代码与原始录屏保留在项目中。
'''
(OUT/'测试验收说明.md').write_text(tests,encoding='utf-8-sig')
print('Packaged tutorial guide for',project_name)
