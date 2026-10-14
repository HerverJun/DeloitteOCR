# 类别与数据划分

类别用实际 label_id 和业务说明。Agent 条件标注与人工确认分别记录，不把预测覆盖人工类别。

数据版本只包括固定集合成员。每类至少三个独立来源才发布；同来源图像使用一致 group_id，不能让同源变体同时进入训练和验证。重复文件、类别不足、资料缺失由工具真实约束反馈。

随机种子、encoder、input_size、epochs、device 和 minimum_precision 按工具契约配置。训练排队不等于训练完成，成功 job 没有模型仍需报告缺产出。模型切换需要已见 expected_active_model_id，模型包需原生校验。
