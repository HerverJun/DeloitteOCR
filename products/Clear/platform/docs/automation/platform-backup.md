# 工作台数据冷备份与恢复

本流程备份三个数据目录：平台工作空间、OCR 原生项目、分类原生项目。程序包和运行时另由候选包提供。操作前停止平台、OCR 和分类服务，确认没有正在运行的原生任务；三份备份必须在同一次停服窗口完成。

在平台仓库根目录运行以下命令。`$platformData`、`$ocrData`、`$classificationData` 指向当前安装实际使用的数据目录；`$classificationTool` 指向随分类应用提供的 `image_platform/backup.py`。`$backupRoot` 必须是全新目录，并位于三个源目录之外。

```powershell
New-Item -ItemType Directory -Path $backupRoot
python scripts/backup_workspace.py create --kind platform --data-dir $platformData --output (Join-Path $backupRoot 'platform')
python scripts/backup_workspace.py create --kind ocr --data-dir $ocrData --output (Join-Path $backupRoot 'ocr')
python $classificationTool create --data-dir $classificationData --output (Join-Path $backupRoot 'classification')
python scripts/backup_workspace.py verify --backup (Join-Path $backupRoot 'platform')
python scripts/backup_workspace.py verify --backup (Join-Path $backupRoot 'ocr')
python $classificationTool verify --backup (Join-Path $backupRoot 'classification')
```

任一步失败时，不应把该目录组当作完整备份。平台和 OCR 数据库通过 SQLite backup API 复制，非数据库文件逐件校验 SHA256；分类备份还校验并收集外部引用原件。备份不包含平台源码、Node 依赖、Python/模型运行时或许可证，这些属于候选程序包。

恢复只写入新的 `$restoreRoot`，不覆盖原数据。恢复后先检查三个数据目录和已安装候选包的配置，再启动服务；旧数据与旧程序包应保留到新候选验证完成。

```powershell
New-Item -ItemType Directory -Path $restoreRoot
python scripts/backup_workspace.py restore --backup (Join-Path $backupRoot 'platform') --target (Join-Path $restoreRoot 'platform-data')
python scripts/backup_workspace.py restore --backup (Join-Path $backupRoot 'ocr') --target (Join-Path $restoreRoot 'ocr-data')
python $classificationTool restore --backup (Join-Path $backupRoot 'classification') --target (Join-Path $restoreRoot 'classification-data')
```

同机隔离演练已从上述三个新目录读回工作空间原件、数据会话、成果、OCR 文档/页/结果、分类项目及模型。候选包统一启动、独立设备搬迁和程序版本回退尚未验证；这些步骤完成前不得把该流程标为完整交付资格。
