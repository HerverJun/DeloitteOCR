# 工作台数据冷备份与恢复

本流程备份三个数据目录：平台工作空间、OCR 原生项目、分类原生项目。程序包和运行时另由候选包提供。操作前停止平台、OCR 和分类服务，确认没有正在运行的原生任务；三份备份必须在同一次停服窗口完成。

在 F 盘平台仓库根目录运行以下命令。`$platformData`、`$ocrData`、`$classificationData` 指向当前安装实际使用的数据目录；`$classificationTool` 指向随分类应用提供的 `image_platform/backup.py`。所有路径、真实链接目标及 Git 元数据都必须位于本机 F 盘。`$backupRoot` 必须是全新目录，并位于三个源目录之外；`$taskTempRoot`、`$taskAppDataRoot` 为本次冷备份专用的 F 盘目录。使用已登记的 Python 运行时，不能依赖盘外默认缓存或临时目录。

```powershell
New-Item -ItemType Directory -Force -Path $taskTempRoot, $taskAppDataRoot | Out-Null
$env:TEMP = $taskTempRoot
$env:TMP = $taskTempRoot
$env:APPDATA = $taskAppDataRoot
$env:LOCALAPPDATA = $taskAppDataRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
New-Item -ItemType Directory -Path $backupRoot
python scripts/backup_workspace.py create --kind platform --data-dir $platformData --output (Join-Path $backupRoot 'platform')
python scripts/backup_workspace.py create --kind ocr --data-dir $ocrData --output (Join-Path $backupRoot 'ocr')
python $classificationTool create --data-dir $classificationData --output (Join-Path $backupRoot 'classification')
python scripts/backup_workspace.py verify --backup (Join-Path $backupRoot 'platform')
python scripts/backup_workspace.py verify --backup (Join-Path $backupRoot 'ocr')
python $classificationTool verify --backup (Join-Path $backupRoot 'classification')
```

任一步失败时，不应把该目录组当作完整备份。平台和 OCR 数据库通过 SQLite backup API 复制，非数据库文件逐件校验 SHA256；分类备份还校验并收集外部引用原件。备份不包含平台源码、Node 依赖、Python/模型运行时或许可证，这些属于候选程序包。

平台备份包含 `screen-batches.sqlite3` 与 `product-reader-requirements.json`，保存原屏拍批次、固定升级子集、已解析策略及托管导出身份。已有屏拍引用而源目录缺少 `screen-batch-v1` 读取能力时，只在私有备份阶段补入能力标记，不修改业务源目录；已有标记的其他能力和属性会保留。校验拒绝能力标记未纳入清单的屏拍备份。平台备份不递归包含 `native` 目录，因此 OCR、分类仍须分别备份。

2026-10-07 的 F 盘隔离恢复检查使用生产平台存储与确定性原生 HTTP 回执，验证五张屏拍表、原批次和升级子集身份、导出 ZIP SHA256、请求去重及读取能力保留。它证明持久化与恢复契约，不作为 OCR 准确率证据。

恢复只写入新的 `$restoreRoot`，不覆盖原数据。恢复后先检查三个数据目录和已安装候选包的配置，再启动服务；旧数据与旧程序包应保留到新候选验证完成。

新候选应先通过只读数据兼容检查。带 `screen-batch-v1` 数据不能交给缺少该读取能力的旧包；OCR schema 18 同样要求候选声明支持该版本。关闭 Agent 只是功能回退。程序回退必须停服后将升级前的三份冷备份恢复到新的 F 盘目录，再由相应旧包打开；升级后新增数据留在原目录，不自动与旧备份合并。Agent 候选通过 `launch.py --data <F盘目录>` 启动，与旧基础候选的安装环境变量流程不同。

```powershell
New-Item -ItemType Directory -Path $restoreRoot
python scripts/backup_workspace.py restore --backup (Join-Path $backupRoot 'platform') --target (Join-Path $restoreRoot 'platform-data')
python scripts/backup_workspace.py restore --backup (Join-Path $backupRoot 'ocr') --target (Join-Path $restoreRoot 'ocr-data')
python $classificationTool restore --backup (Join-Path $backupRoot 'classification') --target (Join-Path $restoreRoot 'classification-data')
```

同机隔离演练已从上述三个新目录读回工作空间原件、数据会话、成果、OCR 文档/页/结果、分类项目及模型。候选包统一启动、独立设备搬迁和程序版本回退尚未验证；这些步骤完成前不得把该流程标为完整交付资格。
