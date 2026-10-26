export const taskIntents = [
  { id: 'explain', title: '解释与整理', description: '读取资料，概括重点并解释内容。', draft: '请帮我阅读资料、概括重点，并解释需要进一步核实的内容。' },
  { id: 'files', title: '文件与表格', description: '清洗数据、计算和生成可下载文件。', draft: '请帮我处理文件或表格，保留编号与金额精度，并生成可下载的结果。' },
  { id: 'audit', title: '审计与核对', description: '对照来源，整理差异和底稿草稿。', draft: '请帮我对照资料来源进行核对，列出差异，并整理有来源的底稿草稿。' },
] as const
