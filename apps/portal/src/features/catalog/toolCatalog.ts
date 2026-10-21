/** Navigation descriptions are capabilities, not live service-readiness claims. */
export const toolCatalog = [
  { id: 'ocr', title: '文档识别', brand: 'ClearOCR', path: '/apps/ocr/start', summary: '识别文字与表格，对照原件校对', formats: 'PDF / 图片' },
  { id: 'data', title: '数据清洗与比对', brand: 'ClearData', path: '/apps/data', summary: '清洗表格数据，核对差异与来源', formats: 'Excel / CSV' },
  { id: 'classification', title: '图像分类', brand: 'ClearClassify', path: '/apps/classification/start', summary: '整理图像资料，分类与人工复核', formats: '图片资料' },
  { id: 'forensics', title: '图像取证', brand: 'ClearForensics', path: '/apps/forensics', summary: '检查可疑线索，查看热图与报告', formats: '图片资料' },
  { id: 'face', title: '人脸比对', brand: 'Face comparison', path: '/apps/face', summary: '比对静态照片，检索项目参考库', formats: '人脸照片' },
  { id: 'audio', title: '音频分析', brand: 'Audio analysis', path: '/apps/audio', summary: '转写中文录音，回听与修订片段', formats: '录音文件' },
] as const
