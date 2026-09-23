"""Write chapter narration and a Windows offline speech synthesis script."""
import json
from pathlib import Path
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/tutorial'
CHAPTERS = [
('01-create','01 / 新建三分类项目',[
 '这段教学从一个全新的分类项目开始。平台已经安装并启动，本机也已缓存预训练权重。',
 '点击新建项目，填写名称和说明。每行定义一个英文类别编号和中文名称，这里使用牛眼菊、玫瑰和向日葵。']),
('02-empty','02 / 确认空项目',[
 '点击创建项目，进入图像与标注。现在项目没有图片，也没有已经训练好的分类模型。',
 '本次使用牛津大学公开的花卉数据，共准备九十张真实照片。六十张用于准备模型，另外三十张保留到训练完成后再分类。']),
('03-import','03 / 按类别文件夹导入',[
 '点击导入文件夹，填写本机的完整目录。这里选择教学数据中的，零一准备目录。',
 '这个目录有三个类别文件夹，名称与类别编号一致。保持按子文件夹匹配类别勾选，平台就能自动填写标签。']),
('04-images','04 / 检查导入结果',[
 '点击开始导入，任务会在后台完成。回到图像与标注，检查真实照片和类别。',
 '这里导入了六十张，其中五十九张自动标注。我们特意把一张玫瑰放进未标注目录，用来演示人工标注。']),
('05-label','05 / 补充人工标注',[
 '把类别筛选改为未标注，勾选这张图片，选择玫瑰，然后点击保存标注。',
 '再切回全部类别。现在三类各有二十张。真实业务中也应检查错误标签；同一对象的多张照片应设置相同来源分组，避免跨集合泄漏。']),
('06-freeze','06 / 冻结数据版本',[
 '进入训练与实验，点击发布数据版本。填写版本名称，保留划分种子四十二，再冻结并发布。',
 '六十张照片被固定划分为三十六张训练、十二张验证、十二张测试。以后修改标签，也不会改变这个已发布版本。']),
('07-configure','07 / 配置模型',[
 '选择通用视觉编码器，迪诺二，并把分类头改为逻辑回归。这里保留二百二十四像素和英伟达显卡。',
 '平台冻结预训练编码器，只训练分类头。逻辑回归的正则化参数只根据验证集选择，因此不需要设置训练轮数。']),
('08-train','08 / 实际训练与 ONNX 导出',[
 '点击开始训练并导出模型。任务会提取图像特征，训练分类头，评估模型，并校验导出结果。',
 '画面保留本机实际等待时间。预训练权重已缓存，首次在另一台电脑使用时可能需要下载，所以耗时会更长。']),
('09-evaluate','09 / 查看评估结果',[
 '任务完成后进入模型库，点击评估详情，查看独立测试集的准确率、各类别指标和混淆矩阵。',
 '注意，这里只有十二张内部测试图片，数字不能当作业务效果保证。下面还会使用另外三十张照片做一次独立分类。']),
('10-model','10 / 下载模型包',[
 '关闭详情，点击下载，保存模型压缩包。里面包含导出模型、类别、预处理配置和评估记录。',
 '这个新训练的模型已作为当前项目的默认模型，后面创建分类任务时可以直接选择它。']),
('11-unseen','11 / 导入独立待分类照片',[
 '回到图像与标注，再导入教学数据中的，零二分类目录。这里是三十张来自官方测试集合的不同照片。',
 '目录里不带类别子文件夹，图片会保持未标注。这批图片没有参与本项目的训练、选参数或阈值设置。']),
('12-scope','12 / 创建批量分类任务',[
 '导入完成后，点击新建分类任务。确认模型版本，并把图片范围保持为未标注图片，只处理刚导入的三十张。',
 '这里使用显卡和默认批大小。保持自动接受关闭，让所有预测先进入人工复核。']),
('13-classify','13 / 查看真实分类结果',[
 '点击开始分类，等待任务完成，再打开详情。下方会显示每张照片的预测类别和分数。',
 '这里的分数不是正确率保证。待复核表示需要人工确认，并不代表图片处理失败；任务的完成数量和失败数量在上方查看。']),
('14-review','14 / 人工检查类别',[
 '点击查看并确认，打开原图检查内容。确认下拉框中的类别，发现预测错误时也可以在这里修改。',
 '本次演示按照照片的真实类别进行确认。建议在正式使用时优先复核低分、边界样本和容易混淆的类别。']),
('15-save','15 / 保存复核结果',[
 '点击确认并保存，图片会增加人工标注。原来的模型预测仍然保留，方便比较和追溯。',
 '导出时，原始预测标签和当前标注标签分别保存；有效标签优先使用当前标注，尚未标注的图片则使用原始预测。']),
('16-export','16 / 导出 CSV 与 JSONL',[
 '点击结果上方的两个导出按钮，分别下载表格文件和逐行数据文件。两种格式都包含本任务的全部三十条记录。',
 '表格适合人工整理，逐行数据适合程序接入。每条结果都带模型版本和图像信息，便于后续核对。']),
('17-reload','17 / 刷新后继续使用',[
 '刷新页面，再进入分类与复核，已完成的任务和结果仍然存在。教学项目会保留在平台，方便你继续操作。',
 '以后增加新类别或纠正更多标签时，重新发布数据版本并训练新模型。教学数据、模型、结果和操作说明已一并保存。']),
]
OUT.mkdir(parents=True, exist_ok=True)
(OUT/'audio').mkdir(exist_ok=True)
chapters=[dict(id=i,title=t,sentences=s) for i,t,s in CHAPTERS]
(OUT/'narration-source.json').write_text(json.dumps(chapters,ensure_ascii=False,indent=2),encoding='utf-8')
script = '''$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('Microsoft Huihui Desktop')
$synth.Rate = 1
$synth.Volume = 100
$chapters = Get-Content -LiteralPath '%s' -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($chapter in $chapters) {
  for ($i=0; $i -lt $chapter.sentences.Count; $i++) {
    $target = Join-Path '%s' ($chapter.id + '-' + $i + '.wav')
    $synth.SetOutputToWaveFile($target)
    $synth.Speak($chapter.sentences[$i])
    $synth.SetOutputToNull()
  }
  Write-Output $chapter.id
}
$synth.Dispose()
''' % (OUT/'narration-source.json', OUT/'audio')
(OUT/'synthesize.ps1').write_text(script,encoding='utf-8-sig')
print('Narration source and offline speech script prepared.')
if '--synthesize' in sys.argv:
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(OUT/'synthesize.ps1')], check=True)
    for chapter in chapters:
        chapter['clips'] = []
        for i, sentence in enumerate(chapter['sentences']):
            audio = OUT/'audio'/f"{chapter['id']}-{i}.wav"
            with wave.open(str(audio)) as wav:
                duration = wav.getnframes()/wav.getframerate()
            chapter['clips'].append(dict(path=str(audio), text=sentence, duration=duration))
        chapter['duration'] = 0.4+sum(clip['duration']+0.35 for clip in chapter['clips'])
    (OUT/'narration.json').write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding='utf-8')
