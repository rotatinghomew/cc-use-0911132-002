"""迁移研究的核心计算引擎。

输出严格区分四类对象：

1. **相邻观测转移（transitions）**：同一规则下相邻两次走访构成的转移对，
   含同档停留、升降档计数与 Wilson 区间；跨多档的观测对只登记为区间删失
   跳变，不虚构中间转移；
2. **窗内端点归类（endpoint 划分）**：首次升级、回落（含先升后落）、
   仍在观察、退出、无法辨认五类互斥人群，在统一 60 天观察长度下比较；
3. **累计发生曲线（cumulative_incidence）**：统一时间网格上的区间删失
   累计比例，上下界分列；
4. **规则平行对照（comparability）**：同一届次分别在旧、新规则下重建的
   结论，只做配对比较，绝不拼接跨规则状态。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from brand_ladder_observatory.transition_study.model import (
    TIER_ORDER,
    TIER_RANK,
    DataQuality,
    MerchantPath,
    StudyInput,
    build_path,
    iso,
)
from brand_ladder_observatory.transition_study.stats import (
    BOOTSTRAP_REPLICATES,
    SMALL_SAMPLE_THRESHOLD,
    bootstrap_paired_difference,
    cumulative_grid,
    direct_standardization,
    pooled_weights,
    total_variation_distance,
    wilson_interval,
)

GRID_DAYS = (30, 45, 60)
ENDPOINT_LABELS = (
    "first_upgrade",
    "upgrade_then_fallback",
    "no_change_observed",
    "exited",
    "unidentifiable",
)


def _round(value: Any, digits: int = 6) -> Any:
    return round(value, digits) if isinstance(value, float) else value


def build_all_paths(study: StudyInput, quality: DataQuality) -> dict[str, dict[str, list[MerchantPath]]]:
    """对每个届次 × 每套规则重建全部商户路径。"""
    mixed_set = set(quality.native_version_mixes)
    paths: dict[str, dict[str, list[MerchantPath]]] = defaultdict(lambda: defaultdict(list))
    for merchant in sorted(study.merchants.values(), key=lambda item: item.merchant_id):
        censor_time = min(
            merchant.entered_at + timedelta(days=study.horizon_days),
            study.as_of,
        )
        for version in (study.reference_rule, study.alternate_rule):
            path = build_path(
                merchant=merchant,
                rule=study.rules[version],
                censor_time=censor_time,
                horizon_days=study.horizon_days,
                recency_tolerance_days=study.recency_tolerance_days,
                mixed_native_versions=merchant.merchant_id in mixed_set,
            )
            paths[merchant.cohort][version].append(path)
    return {cohort: dict(rule_map) for cohort, rule_map in sorted(paths.items())}


def _pair_transition_matrix(paths: list[MerchantPath]) -> dict[str, Any]:
    """相邻观测转移矩阵；不规则间隔按实际天数登记，不做等距假设。"""
    counts: Counter[tuple[str, str]] = Counter()
    gap_days: list[float] = []
    multistep: list[dict[str, Any]] = []
    indeterminate_pairs = 0
    for path in paths:
        for earlier, later in zip(path.points, path.points[1:]):
            counts[(earlier.state, later.state)] += 1
            gap = (later.observed_at - earlier.observed_at).total_seconds() / 86400.0
            gap_days.append(gap)
            if abs(TIER_RANK[later.state] - TIER_RANK[earlier.state]) > 1:
                indeterminate_pairs += 1
        multistep.extend(path.multistep_pairs)
    rows = []
    for source in TIER_ORDER:
        row_total = sum(count for (before, _), count in counts.items() if before == source)
        if row_total == 0:
            continue
        for target in TIER_ORDER:
            count = counts.get((source, target), 0)
            interval = wilson_interval(count, row_total)
            rows.append({
                "from_state": source,
                "to_state": target,
                "count": count,
                "row_total": row_total,
                "probability": interval["estimate"],
                "ci95_lower": interval["lower"],
                "ci95_upper": interval["upper"],
            })
    gap_summary = None
    if gap_days:
        gap_summary = {
            "min_days": _round(min(gap_days), 3),
            "median_days": _round(_median(gap_days), 3),
            "max_days": _round(max(gap_days), 3),
            "irregular_spacing": len(set(round(g, 3) for g in gap_days)) > 1,
        }
    return {
        "pair_count": sum(counts.values()),
        "matrix": rows,
        "gap_days": gap_summary,
        "multistep_jump_pairs": multistep,
        "indeterminate_pair_count": indeterminate_pairs,
        "interval_censoring": "跨多档观测对只登记起止档位，中间转移不可辨认，未做插补",
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _endpoint_partition(paths: list[MerchantPath]) -> dict[str, Any]:
    """窗内互斥端点划分，含无法辨认人群与 Wilson 区间。"""
    total = len(paths)
    assessable = [path for path in paths if path.assessable]
    label_counts = Counter(path.endpoint_label for path in paths)
    partition = []
    for label in ENDPOINT_LABELS:
        count = label_counts.get(label, 0)
        interval = wilson_interval(count, total)
        partition.append({
            "label": label,
            "count": count,
            "share_of_enrolled": interval["estimate"],
            "ci95_lower": interval["lower"],
            "ci95_upper": interval["upper"],
        })
    assessable_rate = wilson_interval(len(assessable), total)
    return {
        "enrolled": total,
        "assessable": len(assessable),
        "unidentifiable": label_counts.get("unidentifiable", 0),
        "assessable_rate": assessable_rate["estimate"],
        "assessable_rate_ci95": [assessable_rate["lower"], assessable_rate["upper"]],
        "partition": partition,
    }


def _time_to_first_upgrade(paths: list[MerchantPath]) -> dict[str, Any]:
    intervals = [path.first_upgrade_interval for path in paths if path.first_upgrade_interval]
    if not intervals:
        return {
            "upgraders": 0,
            "interval_censored_note": "升级时点仅知位于相邻两次走访之间，不做中点插补",
            "median_time_days": None,
            "median_bounds_days": None,
        }
    lower_median = _median([lower for lower, _ in intervals])
    upper_median = _median([upper for _, upper in intervals])
    return {
        "upgraders": len(intervals),
        "interval_censored_note": "升级时点仅知位于相邻两次走访之间，不做中点插补",
        "median_time_days": None,
        "median_bounds_days": [_round(lower_median, 3), _round(upper_median, 3)],
        "individual_bounds_days": [
            {"merchant_id": path.merchant_id,
             "lower_days": _round(path.first_upgrade_interval[0], 3),
             "upper_days": _round(path.first_upgrade_interval[1], 3)}
            for path in paths if path.first_upgrade_interval
        ],
    }


def _transition_block(paths: list[MerchantPath], horizon_days: int) -> dict[str, Any]:
    enrolled = len(paths)
    upgrade_intervals = [
        path.first_upgrade_interval for path in paths if path.first_upgrade_interval
    ]
    fallback_intervals = [
        path.first_fallback_interval for path in paths if path.first_fallback_interval
    ]
    exit_intervals = [
        (
            max(0.0, (path.exit_at - path.origin).total_seconds() / 86400.0),
            (path.exit_at - path.origin).total_seconds() / 86400.0,
        )
        for path in paths
        if path.exit_at is not None
    ]
    return {
        "pair_transitions": _pair_transition_matrix(paths),
        "endpoint_partition": _endpoint_partition(paths),
        "time_to_first_upgrade": _time_to_first_upgrade(paths),
        "cumulative_incidence": {
            "first_upgrade": cumulative_grid(upgrade_intervals, GRID_DAYS, horizon_days, enrolled),
            "first_fallback": cumulative_grid(fallback_intervals, GRID_DAYS, horizon_days, enrolled),
            "exit": cumulative_grid(exit_intervals, GRID_DAYS, horizon_days, enrolled),
        },
    }


def _cohort_block(cohort: str, rule_paths: dict[str, list[MerchantPath]],
                  study: StudyInput) -> dict[str, Any]:
    reference_paths = rule_paths[study.reference_rule]
    sectors = Counter(path.sector for path in reference_paths)
    entry_tiers = Counter(
        path.points[0].state for path in reference_paths if path.points
    )
    origins = [path.origin for path in reference_paths]
    return {
        "cohort": cohort,
        "enrolled": len(reference_paths),
        "entry_window": {
            "first_entered_at": iso(min(origins)),
            "last_entered_at": iso(max(origins)),
        },
        "sector_mix": dict(sorted(sectors.items())),
        "entry_tier_mix_under_reference_rule": dict(sorted(entry_tiers.items())),
        "horizon_days": study.horizon_days,
        "recency_tolerance_days": study.recency_tolerance_days,
        "assumptions": _cohort_assumptions(reference_paths),
        "comparable_without_caution": _cohort_comparable_flag(reference_paths),
    }


def _cohort_assumptions(paths: list[MerchantPath]) -> list[str]:
    return [
        "时间原点取各商户 entered_at；行政删失地为 min(entered_at+horizon_days, as_of)，观察长度统一",
        "仅使用 available_at 不晚于 as_of 的走访与退出事实，晚到事实不采用",
        "状态路径按单一规则从 features 统一重建，不拼接不同 rule_version 的原生 state",
        "失联（无新近走访且无确认退出）计入 unidentifiable，不推断为注销、不向前填充",
        "相邻观测跨多档时只登记区间删失跳变，不虚构中间转移与时点",
        "退出为竞争事件：退出前已观测的升级仍计入升级，退出后不再有状态推断",
    ]


def _cohort_comparable_flag(paths: list[MerchantPath]) -> bool:
    if len(paths) < SMALL_SAMPLE_THRESHOLD:
        return False
    if any(path.mixed_native_versions for path in paths):
        return False
    unidentifiable = sum(1 for path in paths if not path.assessable)
    return unidentifiable / len(paths) <= 0.2


def _stratum_key(path: MerchantPath) -> str:
    entry_tier = path.points[0].state if path.points else "unknown_entry"
    return f"{entry_tier}|{path.sector}"


def _rule_comparison(cohort: str, rule_paths: dict[str, list[MerchantPath]],
                     study: StudyInput, reference_weights: dict[str, float]) -> dict[str, Any]:
    old_paths = rule_paths[study.reference_rule]
    new_paths = rule_paths[study.alternate_rule]
    by_id_old = {path.merchant_id: path for path in old_paths}
    by_id_new = {path.merchant_id: path for path in new_paths}
    paired = sorted(set(by_id_old) & set(by_id_new))

    discordant: list[dict[str, Any]] = []
    paired_upgrade: list[tuple[bool, bool]] = []
    paired_fallback: list[tuple[bool, bool]] = []
    for merchant_id in paired:
        old = by_id_old[merchant_id]
        new = by_id_new[merchant_id]
        paired_upgrade.append((old.ever_upgraded, new.ever_upgraded))
        paired_fallback.append((old.ever_fell_back, new.ever_fell_back))
        if old.endpoint_label != new.endpoint_label or old.ever_upgraded != new.ever_upgraded:
            discordant.append({
                "merchant_id": merchant_id,
                "reference_endpoint": old.endpoint_label,
                "alternate_endpoint": new.endpoint_label,
                "reference_ever_upgraded": old.ever_upgraded,
                "alternate_ever_upgraded": new.ever_upgraded,
            })

    # 按入驻结构（入组档位×行业）标准化的首次升级率。层名统一采用参考规则
    # 下的入组档位划分，保证两套规则用同一套参考权重对齐；备选规则路径上
    # 的商户同样按其参考规则入组档位归层（同一商户的参考入组档在两路径间
    # 固定），避免规则重述改变层归属造成假对齐。参考权重中某层在备选规则
    # 下无观测时，显式登记缺失并报告被覆盖的权重比例。
    reference_entry_tier = {
        path.merchant_id: (path.points[0].state if path.points else "unknown_entry")
        for path in old_paths
    }

    def stratum_upgrade_rate(rule_paths_for_rule: list[MerchantPath]) -> dict[str, float | None]:
        buckets: dict[str, list[bool]] = defaultdict(list)
        for path in rule_paths_for_rule:
            key = f"{reference_entry_tier[path.merchant_id]}|{path.sector}"
            buckets[key].append(path.ever_upgraded)
        rates: dict[str, float | None] = {}
        for key in reference_weights:
            hits = buckets.get(key)
            rates[key] = (sum(hits) / len(hits)) if hits else None
        return rates

    standardized_old = direct_standardization(stratum_upgrade_rate(old_paths), reference_weights)
    standardized_new = direct_standardization(stratum_upgrade_rate(new_paths), reference_weights)
    bootstrap_upgrade = bootstrap_paired_difference(paired_upgrade, BOOTSTRAP_REPLICATES)
    bootstrap_fallback = bootstrap_paired_difference(paired_fallback, BOOTSTRAP_REPLICATES)

    old_partition = Counter(path.endpoint_label for path in old_paths)
    new_partition = Counter(path.endpoint_label for path in new_paths)
    endpoint_tvd = total_variation_distance(
        {label: old_partition.get(label, 0) / len(old_paths) for label in ENDPOINT_LABELS},
        {label: new_partition.get(label, 0) / len(new_paths) for label in ENDPOINT_LABELS},
    )
    return {
        "cohort": cohort,
        "reference_rule": study.reference_rule,
        "alternate_rule": study.alternate_rule,
        "reconstruction": "两套规则分别从同一批 features 重建完整路径，未拼接跨规则 state",
        "paired_merchants": len(paired),
        "discordant_merchants": discordant,
        "discordant_count": len(discordant),
        "crude_first_upgrade_rate": {
            "reference": bootstrap_upgrade["old_rate"],
            "alternate": bootstrap_upgrade["new_rate"],
        },
        "standardized_first_upgrade_rate": {
            "reference": standardized_old,
            "alternate": standardized_new,
            "standardization_strata": "entry_tier_x_sector",
            "reference_weights": reference_weights,
        },
        "paired_difference_reference_minus_alternate": {
            "first_upgrade": bootstrap_upgrade,
            "first_fallback": bootstrap_fallback,
        },
        "endpoint_distribution_tvd": endpoint_tvd,
        "comparable": len(discordant) == 0 and endpoint_tvd < 0.2,
        "caution_reasons": _comparison_cautions(old_paths, new_paths, discordant),
    }


def _comparison_cautions(old_paths: list[MerchantPath], new_paths: list[MerchantPath],
                         discordant: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if discordant:
        reasons.append("两套规则对同一商户的端点或升级结论存在分歧，差异来自标准切换而非经营变化")
    if any(path.mixed_native_versions for path in old_paths):
        reasons.append("部分商户原生走访混用 rule_version，结论完全依赖 features 重建，需核对特征口径")
    if any(not path.assessable for path in old_paths):
        reasons.append("存在无法辨认人群，规则对照仅覆盖 assessable 商户的可辨识部分")
    if len(old_paths) < SMALL_SAMPLE_THRESHOLD:
        reasons.append(f"届次样本量小于 {SMALL_SAMPLE_THRESHOLD}，bootstrap 区间不稳定，不宜外推")
    return reasons


def analyze(study: StudyInput, quality: DataQuality) -> dict[str, Any]:
    all_paths = build_all_paths(study, quality)
    cohorts_out: list[dict[str, Any]] = []
    transitions_out: list[dict[str, Any]] = []
    comparability_out: list[dict[str, Any]] = []
    per_merchant_paths: list[dict[str, Any]] = []

    for cohort, rule_paths in all_paths.items():
        cohorts_out.append(_cohort_block(cohort, rule_paths, study))
        reference_paths = rule_paths[study.reference_rule]
        reference_weights = pooled_weights(reference_paths, _stratum_key)
        for version in (study.reference_rule, study.alternate_rule):
            paths = rule_paths[version]
            block = _transition_block(paths, study.horizon_days)
            transitions_out.append({
                "cohort": cohort,
                "rule_version": version,
                "is_reference": version == study.reference_rule,
                **block,
            })
            for path in paths:
                per_merchant_paths.append(_serialize_path(path))
        comparability_out.append(
            _rule_comparison(cohort, rule_paths, study, reference_weights)
        )

    # 跨届次结构差异提示：入驻结构不同的年度不能只比较一个总升级率。
    cross_cohort: dict[str, Any] = {"pairwise": []}
    if len(cohorts_out) > 1:
        tier_structure = {
            block["cohort"]: {
                key: count / block["enrolled"]
                for key, count in block["entry_tier_mix_under_reference_rule"].items()
            }
            for block in cohorts_out
        }
        sector_structure = {
            block["cohort"]: {
                key: count / block["enrolled"]
                for key, count in block["sector_mix"].items()
            }
            for block in cohorts_out
        }
        names = sorted(tier_structure)
        structure_tvd = {}
        for index, left in enumerate(names):
            for right in names[index + 1:]:
                tier_tvd = total_variation_distance(tier_structure[left], tier_structure[right])
                sector_tvd = total_variation_distance(
                    sector_structure[left], sector_structure[right]
                )
                pair_key = f"{left}_vs_{right}"
                structure_tvd[pair_key] = tier_tvd
                structurally_different = tier_tvd > 0.2 or sector_tvd > 0.2
                cross_cohort["pairwise"].append({
                    "pair": pair_key,
                    "entry_tier_tvd": tier_tvd,
                    "sector_tvd": sector_tvd,
                    "structurally_different": structurally_different,
                    "recommendation": (
                        "入驻结构差异显著，禁止只比较总升级率；请使用标准化率或按层对比"
                        if structurally_different
                        else "入驻结构接近，可并列比较届次端点比例，但仍须分别报告区间"
                    ),
                })
        cross_cohort["entry_tier_tvd"] = structure_tvd
    else:
        structure_tvd = {}
    cross_cohort["single_cohort_note"] = (
        "仅一个届次，无跨届次结构比较；总升级率仅描述该届次，不能当作入驻商户的成长概率"
        if len(cohorts_out) == 1 else ""
    )

    uncertainty = {
        "confidence_level": 0.95,
        "proportion_method": "Wilson score interval，小样本或 0/1 比例下区间不越界",
        "bootstrap_replicates": BOOTSTRAP_REPLICATES,
        "bootstrap_seed": 20261030,
        "small_sample_threshold": SMALL_SAMPLE_THRESHOLD,
        "interval_censoring": "升级/回落时点只知位于相邻走访之间，累计曲线给上下界，不做中点插补",
        "unidentifiable_handling": "失联与无观测商户保留为独立类别，不并入退出也不按零事件处理",
    }
    versions = {
        "reference_rule": _rule_dict(study, study.reference_rule),
        "alternate_rule": _rule_dict(study, study.alternate_rule),
        "source_versions": study.source_versions,
        "schema_version": study.schema_version,
        "native_version_merchants": quality.native_version_mixes,
        "state_rebuild_check": {
            "mismatch_count": len(quality.state_mismatches),
            "mismatches": quality.state_mismatches,
        },
    }
    return {
        "cohorts": cohorts_out,
        "transitions": transitions_out,
        "comparability": comparability_out,
        "uncertainty": uncertainty,
        "versions": versions,
        "cross_cohort_comparability": cross_cohort,
        "cross_cohort_structure_tvd": structure_tvd,
        "data_quality": quality.as_dict(),
        "_merchant_paths": per_merchant_paths,
    }


def _rule_dict(study: StudyInput, version: str) -> dict[str, Any]:
    rule = study.rules[version]
    return {
        "version": version,
        "independent_threshold": rule.independent_threshold,
        "regional_threshold": rule.regional_threshold,
    }


def _serialize_path(path: MerchantPath) -> dict[str, Any]:
    return {
        "merchant_id": path.merchant_id,
        "cohort": path.cohort,
        "sector": path.sector,
        "rule_version": path.rule_version,
        "origin": iso(path.origin),
        "censor_time": iso(path.censor_time),
        "endpoint_label": path.endpoint_label,
        "endpoint_detail": path.endpoint_detail,
        "assessable": path.assessable,
        "ever_upgraded": path.ever_upgraded,
        "ever_fell_back": path.ever_fell_back,
        "first_upgrade_interval_days": (
            [_round(path.first_upgrade_interval[0], 3), _round(path.first_upgrade_interval[1], 3)]
            if path.first_upgrade_interval else None
        ),
        "first_fallback_interval_days": (
            [_round(path.first_fallback_interval[0], 3), _round(path.first_fallback_interval[1], 3)]
            if path.first_fallback_interval else None
        ),
        "exit_at": iso(path.exit_at) if path.exit_at else None,
        "exit_reason": path.exit_reason,
        "mixed_native_versions": path.mixed_native_versions,
        "states": [
            {"visit_id": point.visit_id, "observed_at": iso(point.observed_at),
             "state": point.state, "native_rule_version": point.native_rule_version,
             "rebuilt_from_features": point.rebuilt_from_features}
            for point in path.points
        ],
        "multistep_pairs": path.multistep_pairs,
        "note": path.note,
    }
