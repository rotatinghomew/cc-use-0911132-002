# 观测不齐的商户成长迁移与退出分析

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `brand_ladder_observatory.operator.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/brand_mobility -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

迁移研究测试：`python3 -m unittest discover -s tests/brand_mobility -p test_transition_study.py -v`。

构建检查：`python3 -m compileall -q brand_ladder_observatory tests`。

业务调用：`python3 -m brand_ladder_observatory.operator.cli fixtures/demo.json --database /tmp/brand_ladder_observatory-demo.sqlite3`。

## 入驻届次迁移研究

`brand_ladder_observatory.transition_study` 面向入驻届次（cohort）回答“通常多久能从代工走到自主品牌”，不再用当前在册商户的梯队占比充当成长概率。运行方式：

```
python3 -m brand_ladder_observatory.transition_study \
  --input fixtures/analysis/input.json \
  --database <registry.sqlite3> \
  --output <transitions.json>
```

输出顶层字段为 `study_id、cohorts、transitions、uncertainty、comparability、versions`，并附带 `data_quality、cross_cohort_comparability、method_summary、registry`。同一输入重放返回同一 `study_id`（研究级幂等键为输入指纹），分析记录写入同一 SQLite registry 的 `transition_studies` 与 `transition_merchant_paths` 两张新表，不改动既有 `runs/events/idempotency/failures` 表与静态分类、商户查询入口。

### 口径与方法假设（随结果展示）

- 时间原点为各商户 `entered_at`，行政删失地为 `min(entered_at + horizon_days, as_of)`，同一届次在统一观察长度下比较；迟入组商户从自身入驻时刻起算，不向首次进入视野之前填充状态。
- 只有 `available_at <= as_of` 的走访与退出事实可采用；更晚可用的事实登记在 `data_quality.late_*` 并排除。
- 每条规则下的状态路径都用该规则阈值从 `features.own_brand_sales` 统一重建；不同 `rule_version` 的原生 `state` 绝不拼接成一条轨迹。原生走访混用规则版本的商户登记在 `versions.native_version_merchants`。
- 失联不等于注销：无确认退出且末次走访早于窗末宽限期（默认 30 天，可用 `--recency-tolerance-days` 调整）的商户归入 `unidentifiable`，独立成类、不按零事件处理、不推断为退出。
- 相邻两次观测跨多个档位时只登记一次区间删失多档跳变（`multistep_jump_pairs`），中间转移标记 `not_observable`，不做中点插补、不虚构中间状态。
- 退出是竞争事件：退出前已观测到的升级仍计入升级，退出后不再推断状态。
- 窗内端点互斥五类：`first_upgrade`（首次升级、未见回落）、`upgrade_then_fallback`（先升级后回落到更低档）、`no_change_observed`（仍在观察且未变档）、`exited`（窗内确认注册退出）、`unidentifiable`（失联或入组后无有效走访）。
- 比例给 Wilson 95% 区间（小样本与 0/1 比例不越界）；新旧规则的配对差值给固定随机种子的非参数 bootstrap 百分位区间；升级/回落时点只知位于相邻走访之间，累计曲线在统一网格（30/45/60 天）上给 `cumulative_lower/upper` 区间带，分母固定为届次入组总人数。
- 新旧标准比较按同一批商户配对进行，并给按“参考规则入组档位 × 行业”分层的直接标准化升级率；入驻结构差异显著（入组档位或行业分布 TVD > 0.2）的届次禁止只比较一个总升级率，结论写在 `cross_cohort_comparability`。样本量小于 10、存在无法辨认人群或规则结论分歧的届次在 `comparable_without_caution/comparable` 中标记为需谨慎。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：unequal_observation、registry_exits、rule_versions。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证；方法正确性（失联非注销、入组前不填充、多档跳变不插补、规则切换平行重建、跨届次结构差异、小样本与幂等）由 `tests/brand_mobility/test_transition_study.py` 覆盖。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。分析 ID 为 `study_id`，其余输出包括 `cohorts, transitions, uncertainty, comparability, versions`。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
