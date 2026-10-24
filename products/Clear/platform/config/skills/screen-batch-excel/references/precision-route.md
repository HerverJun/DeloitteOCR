# 首次提交时的可选精度策略

仅当用户明确要求本次整批提高精度时使用。按绑定原图业务区域的独立文字证据逐图分流：中文或证据不明用 Hunyuan，明确拉丁文本用 GLM，GLM 技术失败时才兜底 Hunyuan。主控不读文件名、候选值或分数来选模型。耗时增加，结果仍需核对；模拟验证减少识别错误的成绩不等于人工减负或免复核资格。

先核实固定资料集合的真实图片数量。每图原始额度360秒，整批 `batch_budget_seconds=max(36000,图片数*360)`；200、240、1000张分别为72000、86400、360000秒。额度是上限，不是预计耗时。一次提交的策略如下是240张的完整例子，其他数量只按上述公式计算整批原始额度：

```json
{
  "primary_engine": "glm",
  "fallback_engines": ["hunyuan"],
  "max_upgrades": 1,
  "mode": "accuracy",
  "upgrade_trigger": "failure_only",
  "layout_evidence": true,
  "require_human_review": true,
  "budget_seconds": 360,
  "batch_budget_seconds": 86400,
  "request_timeout_seconds": 180,
  "engine_route": "business_language_v1",
  "technical_fallback_reserve_seconds": 90
}
```

90秒是在每图原额度内预留的最低兜底容量，兜底可以使用实际剩余额度；成功识别不会为质量问题再调用另一个模型。单独发送旧的 `mode="accuracy"` 不会选择这条新路线。

未知提交结果、恢复与重复指令先查原回执，保留策略、请求身份、成本和截止时间，不重提整批或增加旧预算。用户后续只提高所选终态图片的精度时，使用原批次控制工具创建固定GLM子批次，原结果保留。
