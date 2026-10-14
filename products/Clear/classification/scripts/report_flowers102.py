"""Render measured public benchmark results and limitations into a reviewable report."""
import json
from pathlib import Path
import statistics


def percent(value):
    return '无接受样本' if value is None else f'{value * 100:.2f}%'


def main():
    data = json.loads(Path('reports/flowers102-benchmark.json').read_text(encoding='utf-8'))
    cpu = json.loads(Path('reports/cpu-inference-benchmark.json').read_text(encoding='utf-8'))
    assert len(data['models']) == 16 and len(data['performance']) == 12 and len(cpu['models']) == 4
    selected = {key: next(m for m in data['models'] if m['model_id'] == model) for key, model in data['recommendations'].items()}
    speed = {(r['encoder'], r['mode']): r['median_images_per_second'] for r in data['performance']}
    cpu_speed = {m['encoder']: m['median_images_per_second'] for m in cpu['models']}
    lines = ['# 真实数据评测与 v0.2 开发结果', '',
             '评测日期：2026-09-18。硬件：RTX 4070 Ti SUPER 16 GB、Intel i5-13600KF；Windows 11 本机；FP32，关闭 TF32。', '',
             '已完成真实数据审计、原基线对照、输入流水线优化、两种轻量骨干接入、验证集正则化搜索和独立 CPU 推理验证。下表为本机实测，不使用原论文不同硬件的速度代替测量。', '',
             '## 数据与实验纪律', '',
             '- 数据：[Oxford Flowers102 官方网站](https://www.robots.ox.ac.uk/~vgg/data/flowers/102/)，8,189 张真实图片、102 类；没有使用生成图片。',
             '- 原始官方划分为训练 1,020、验证 1,020、测试 6,149。校验官方归档 MD5，同时保存 SHA256 和来源。',
             '- 内容哈希检查发现 2 张跨集合完全重复图；视觉复核发现 4 对跨集合近重复图。保留测试集，隔离训练 1 张、验证 5 张；最终训练 **1,019**、验证 **1,015**、测试 **6,149**，项目保留 **8,183** 张有效图片。',
             '- 各类别原计划 10 张训练样本；第 94 类去重后为 9 张。全部类别均覆盖三个集合。去重修订发生在任何模型成功训练前，旧不可变快照仍保留审计。',
             '- AdamW 基线固定 100 轮、学习率 0.03、权重衰减 0.01，种子 42/43/44；冻结编码器。每轮模型只按验证损失选择。',
             '- 逻辑回归比较 C=0.1/1/10/100，只按验证 log loss 选择；每种编码器推荐模型按验证宏平均 F1 选择、准确率打破平局。测试结果不用于选择。',
             '- 三个种子只反映固定划分下分类头初始化的波动，不代表三次独立采样或跨场景稳定性。逻辑回归为确定性求解，运行一次。',
             '- 仍采用平台统一的白底 letterbox 输入；这是平台内公平对照，不是复现各原论文的 ImageNet 配方。', '',
             '## 准确率与分类头对照', '',
             '| 编码器 | AdamW 测试准确率，三种子均值 ± 样本 SD | 正则化头验证准确率 | 正则化头测试准确率 | 正则化头测试宏 F1 | 验证选择 C |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for encoder in selected:
        adam = [m for m in data['models'] if m['params']['encoder'] == encoder and m['params']['head_method'] == 'adamw']
        logistic = next(m for m in data['models'] if m['params']['encoder'] == encoder and m['params']['head_method'] == 'logistic_cv')
        values = [m['metrics']['test']['accuracy'] * 100 for m in adam]
        metrics = logistic['metrics']
        lines.append(f"| {encoder} | {statistics.mean(values):.2f}% ± {statistics.stdev(values):.2f} pp | {percent(metrics['validation']['accuracy'])} | {percent(metrics['test']['accuracy'])} | {percent(metrics['test']['macro_f1'])} | {metrics['selected_c']} |")
    lines += ['', '## 验证集选出的模型与自动接受策略', '',
              '| 编码器 | 推荐分类头 / 种子 | 测试准确率（95% Wilson 区间） | 测试宏 F1 | 自动接受覆盖率 | 接受结果精确率（95% 区间） |',
              '| --- | --- | ---: | ---: | ---: | ---: |']
    for encoder, model in selected.items():
        metrics = model['metrics']['test']
        ci = '–'.join(percent(x) for x in metrics['accuracy_ci95_wilson'])
        pi = '–'.join(percent(x) for x in metrics['accepted_precision_ci95_wilson']) if metrics['accepted_precision_ci95_wilson'] else '无'
        lines.append(f"| {encoder} | {model['params']['head_method']} / {model['params']['seed']} | {percent(metrics['accuracy'])}（{ci}） | {percent(metrics['macro_f1'])} | {percent(metrics['coverage'])} | {percent(metrics['accepted_precision'])}（{pi}） |")
    missed_target = [encoder for encoder, model in selected.items() if (model['metrics']['test']['accepted_precision'] or 0) < 0.95]
    if missed_target:
        lines += ['', '本次 **' + '、'.join(missed_target) + '** 的测试接受精确率未达到验证阶段设定的 95% 目标，说明有限验证集上的阈值不能直接当作自动归档保证。']
    lines += ['', '阈值用有限验证样本拟合，目标精确率为 95%；目标不等于测试表现保证。当前仍允许每类仅 3 个接受样本，统计支持不足：3/3 正确的 Wilson 95% 下界约 43.85%。默认业务推理继续全部待复核。区间仅描述固定测试集的二项比例，没有覆盖模型选择、多类别比较或近似同源图片引起的相关性。', '',
              '主要误分（真实类别 → 预测类别，括号为张数；完整每类指标和矩阵见 JSON）：', '']
    for encoder, model in selected.items():
        metrics = model['metrics']['test']
        matrix, labels = metrics['confusion_matrix'], metrics['label_order']
        errors = sorted(((value, labels[i], labels[j]) for i, row in enumerate(matrix) for j, value in enumerate(row) if i != j and value), reverse=True)[:5]
        lines.append(f"- {encoder}：" + '；'.join(f'{a} → {b}（{n}）' for n, a, b in errors))
    lines += ['',
              '## 速度与模型体积', '',
              '| 编码器 | 完整 ONNX MiB | v0.1 原流水线 图/秒 | 优化版新建会话 图/秒 | 优化版复用会话 图/秒 | 复用会话 / 原版 | 独立 CPU 图/秒 |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for encoder, model in selected.items():
        old, cold, warm = (speed[(encoder, mode)] for mode in ['archived', 'optimized_cold', 'optimized_warm'])
        lines.append(f"| {encoder} | {model['onnx_mib']:.2f} | {old:.1f} | {cold:.1f} | {warm:.1f} | {warm/old:.2f}× | {cpu_speed[encoder]:.1f} |")
    lines += ['', 'GPU 口径：固定测试集中按内容 SHA 排序的前 1,024 张，batch 32，每种组合重复 3 次取吞吐中位数；计时含包校验、会话初始化（或命中）、读图、哈希、解码、归一化、GPU 拷贝/计算、决策与 SQLite 落库，不含排队等待及任务清单创建。使用每个编码器的 AdamW seed 42 模型，参数和全部记录见 JSON。表中准确率推荐模型可能使用另一分类头，编码器和图结构同级，仍不把计时当作该推荐 checkpoint 的直接计时。',
              '“新建会话”只表示 ORT session 重新创建，不代表操作系统磁盘缓存或进程冷启动。文件缓存已经受前序实验影响。顺序为原版、新建会话、复用会话，各 3 次；没有做随机交错或统计显著性检验。速度是这组本地 JPEG 的吞吐，不是云服务 p95 或 15 万张业务图片验收。',
              '全部对照检查 1,024 个输出的类别和接受状态一致、分数在指定浮点容差内。预处理本身与归档版本逐元素完全一致。新流水线改用有界分块读取，避免小图片申请 100 MiB 读取缓冲区，使用 4 个解码线程、只预取下一批、最多缓存 1 个会话，并批量更新任务计数；暂停恢复、坏图、OOM 降批路径均有测试。',
              'CPU 口径：无 PyTorch/timm/CUDA 依赖的独立环境，ORT CPU 4 线程、batch 16，固定前 128 张真实测试图片、3 次；计时含读取/校验/预处理/推理，不含模型初始化、决策序列化、SQL 或结果写文件，因此不能直接与 GPU 表列相除。记录另含初始化时间及端到端批次 p50/p95。', '',
              f"独立 CPU `.venv-inference` 实占 **{cpu['environment_bytes']/1024**2:.1f} MiB**；开发 GPU `.venv` 约 5.10 GiB。前者不包括共享 Python 基础解释器、模型和 pip 缓存，后者也不含模型缓存。推理包需要另装 Python 3.12 和锁定依赖，不是单文件可执行程序。", '',
              '## 尚存不足与下一步顺序', '',
              '1. **业务精度优先**：Flowers102 是自然花卉细粒度闭集识别，不能替代文档、商品或工业验收。下一轮加入用户场景、独立来源/时间划分，以及未知类、模糊、裁切、旋转、光照变化测试。无法排除公共图片曾出现在预训练语料中的可能。',
              '2. **让轻量模型保住关键类别召回率**：根据本轮准确率/体积/CPU 速度取舍选择骨干，再评估部分解冻、数据增强和强模型蒸馏。不要仅因轻量模型参数少就认定 GPU 端到端更快。',
              '3. **改进自动接受校准**：增加独立校准样本；引入精确率置信下界约束，报告覆盖率—错误率曲线和未知类拒识。界面已同时显示覆盖率和接受精确率。',
              '4. **按瓶颈继续优化部署**：阶段计时确认输入和 SQL 成本后，评估 RepViT 重参数化、FP16/INT8 与目标 CPU/GPU 后端；量化必须复测全部类别及阈值一致性。本轮没有宣称完成量化或 TensorRT。',
              '5. **扩展评测与界面**：目前感知近重复检测仅用于公开数据导入脚本，尚未做成全平台交互式审核；前端 JS gzip 约 371 KB，仍可按页拆包。', '',
              '## 可复现入口与证据', '',
              '- 固定方案：`benchmarks/flowers102-protocol.json`；旧代码及哈希：`benchmarks/baseline-v0.1/`。',
              '- 下载：`.venv/Scripts/python.exe scripts/acquire_flowers102.py`；导入：`scripts/import_flowers102.py`。',
              '- 停止后台 worker 且确保队列无其他任务后：`.venv/Scripts/python.exe scripts/benchmark_flowers102.py --phase all`。已成功任务会复用记录；失败任务须检查原因后显式处理。',
              '- CPU 环境安装：Python 3.12 建 venv，`pip install -r requirements-cpu-inference.lock.txt`；设置 `PYTHONPATH=src` 后执行 `scripts/benchmark_cpu_inference.py`。',
              '- 报告：`scripts/report_flowers102.py`；独立包：`scripts/build_cpu_bundle.py --encoder mobilenetv4` 或 `--encoder repvit`。',
              '- 机器记录：[GPU/精度结果](../reports/flowers102-benchmark.json)、[CPU 结果](../reports/cpu-inference-benchmark.json)、[数据审计](../reports/flowers102-data-audit.json)、[近重复复核图](../reports/flowers102-duplicate-candidates.png)、[权重来源](../sources/lightweight-weights.json)。',
              '- 软件回归：31 项 Python 测试、6 项外部 Chromium 浏览器流程通过；包含真实 GPU 分类、人工复核、导出、公开基准界面以及暂停取消。前端构建和两个 Python 环境依赖检查通过；独立推理包已实际执行。验证汇总见 `reports/v02-validation.json`。',
              '- 本报告中的成绩来自开放模型在本平台同一协议下的运行；原始开源方案、许可证和后续候选见 [原技术对标](10-open-source-comparison.md)。', '']
    Path('docs/11-public-benchmark-and-v02.md').write_text('\n'.join(lines), encoding='utf-8')
    print('docs/11-public-benchmark-and-v02.md')


if __name__ == '__main__':
    main()
