export const toolCategories = [
  { id: 'document', label: '文档', title: '文档识别与核对', description: '提取文字与印章，比较内容，排查改字线索。' },
  { id: 'data', label: '表格', title: '表格清洗与比对', description: '整理表格，核对两份数据中的相同项和差异。' },
  { id: 'image', label: '图片', title: '图片检查与整理', description: '检查生成与篡改线索，比对人脸，整理图片类别。' },
  { id: 'audio', label: '录音', title: '录音转写', description: '将录音转成文字，回听原音并修订转写。' },
] as const
export type ToolCategory = typeof toolCategories[number]['id']

/** Navigation descriptions are capabilities, not live service-readiness claims. */
export const toolCatalog = [
  { id: 'ocr', category: 'document', title: '文档识别', brand: 'ClearOCR', path: '/apps/ocr/start', summary: '识别文字与表格，对照原件校对', formats: 'PDF / 图片', keywords: 'OCR 文字识别 扫描 提取 发票 图片转文字', experimental: false },
  { id: 'comparisons', category: 'document', title: '文档主题对比', brand: 'Clear Compare', path: '/apps/comparisons', summary: '比较两份文档中的指定信息，生成对照报告', formats: 'PDF / PPTX / 图片', keywords: '幻灯片 主题 比较 进度 差异 文档有什么不同', experimental: false },
  { id: 'seals', category: 'document', title: '印章识别', brand: 'Seal recognition', path: '/apps/ocr/seals', summary: '定位印章，提取章内文字并核对主体', formats: 'PDF / 图片', keywords: '印章 公章 识别 定位 文字 主体', experimental: false },
  { id: 'document_forensics', category: 'document', title: '文档篡改排查', brand: 'Document forensics', path: '/apps/forensics/document', summary: '排查票据与合同图片中的局部改字线索', formats: '文档图片', keywords: '文档 篡改 排查 改字 定位 原件 对照 复核 金额修改', experimental: false },
  { id: 'data', category: 'data', title: '数据清洗与比对', brand: 'ClearData', path: '/apps/data', summary: '清洗表格数据，核对差异与来源', formats: 'Excel / CSV', keywords: '数据 清洗 比对 核对 Excel CSV 差异 表格 对账', experimental: false },
  { id: 'aigc_image', category: 'image', title: '整图 AI 生成检测', brand: 'AIGC detection', path: '/apps/forensics/aigc', summary: '查看整张图片的 AI 生成相关信号', formats: '图片', keywords: 'AIGC AI 整图 生成 检测 鉴别 文档 信号 来源 是不是AI生成的 合成图片', experimental: true },
  { id: 'forensics', category: 'image', title: '图像取证', brand: 'ClearForensics', path: '/apps/forensics', summary: '检查图片的可疑线索，查看热图与报告', formats: '图片', keywords: '取证 图片 图像 篡改 EXIF 热图 报告 图片有没有改过', experimental: false },
  { id: 'deep_forensics', category: 'image', title: 'SAFIRE 深度定位', brand: 'SAFIRE', path: '/apps/forensics/deep', summary: '定位图片中的可疑区域，与原图对照', formats: 'JPEG / PNG', keywords: 'SAFIRE 深度 定位 图像 图片 取证 篡改 可疑 区域 局部修改', experimental: false },
  { id: 'face', category: 'image', title: '人脸比对', brand: 'Face comparison', path: '/apps/face', summary: '比对静态照片，检索项目参考库', formats: '人脸照片', keywords: '人脸 比对 图片 照片 参考库 相似度 检索 是不是同一个人', experimental: false },
  { id: 'classification', category: 'image', title: '图像分类', brand: 'ClearClassify', path: '/apps/classification/start', summary: '整理图像资料，分类与人工复核', formats: '图片资料', keywords: '分类 图像 归类 训练 标注 复核 整理照片', experimental: false },
  { id: 'audio', category: 'audio', title: '音频分析', brand: 'Audio analysis', path: '/apps/audio', summary: '转写中文录音，回听与修订片段', formats: '录音文件', keywords: '音频 分析 录音 中文 转写 语音 说话人 波形 录音转文字', experimental: false },
] as const
