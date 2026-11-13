# 业务演练数据口径

visits.state 是当次规则输出，features 支持用另一分类规则重述；不得把不同 rule_version 的 state 直接连成历史。exits 仅表示确认的注册退出；没有后续 visit 不表示退出。入组前没有状态记录，不允许向前填充。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
