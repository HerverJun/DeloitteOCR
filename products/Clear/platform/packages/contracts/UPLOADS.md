# 逐文件上传与恢复

新接口为 `POST /api/platform/v1/workspaces/{workspace_id}/assets/uploads/{request_id}`，二进制正文使用 `application/octet-stream`；必填查询参数为 `name`、`sha256`、`size_bytes`。`GET` 同一路径读取原请求状态。旧上传接口保留。

一个请求绑定工作空间、请求 ID、原文件名、字节数和 SHA256。客户端在发送前保存绑定；中断后沿用原 ID，不能为不明结果创建替代请求。首次提交返回 201，成功重放返回 200，均为 `UploadStatus`。每次 POST（包括已成功的重放）验证实际正文，元数据或正文不同不能改变原资产或回执。

`pending` 表示效果待核实；`committed` 必须包含原固定 `UploadReceipt`；`rejected` 必须证明没有登记，错误详情携带 `effect=no_registration`。GET 的 404 不证明没有效果，应保留原绑定再查询或按原 ID 提交。明确未登记的正文拒绝允许同一绑定重试正确原文件。

资产登记和成功回执在同一 SQLite 事务提交。收到回执后，客户端保留成功项，恢复只处理失败、待核实和未提交项。重载页面后文件对象不能恢复，需要重选文件并核对原文件名、大小和 SHA256。

契约唯一来源为 `uploads.py`；schema、OpenAPI 和前端类型由 `packages.contracts.generate` 生成。
