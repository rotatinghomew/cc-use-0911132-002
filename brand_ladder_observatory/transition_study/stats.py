"""小样本友好的统计计算（仅依赖标准库）。

包含：

* Wilson 二项比例区间（相比正态近似，在 n 很小或比例接近 0/1 时不会给出
  越界区间）；
* 统一时间网格上的累计发生比例；
* 以届次入驻结构（入组档位 × 行业）为层的直接标准化；
* 固定随机种子的非参数 bootstrap，用于新旧规则配对差值的区间。
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Sequence

CONFIDENCE_LEVEL = 0.95
SMALL_SAMPLE_THRESHOLD = 10
BOOTSTRAP_REPLICATES = 1000
BOOTSTRAP_SEED = 20261030


def normal_quantile(p: float) -> float:
    """标准正态分位数（Peter Acklam 有理逼近，误差约 1.15e-9）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("分位概率必须位于 (0,1) 区间")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    p_low, p_high = 0.02425, 0.97575
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def wilson_interval(successes: int, trials: int, confidence: float = CONFIDENCE_LEVEL) -> dict[str, Any]:
    """Wilson score 区间；trials 为 0 时返回不可估计标记。"""
    if trials <= 0:
        return {"estimate": None, "lower": None, "upper": None, "trials": 0, "method": "wilson_score"}
    z = normal_quantile(1.0 - (1.0 - confidence) / 2.0)
    phat = successes / trials
    denom = 1.0 + z * z / trials
    center = (phat + z * z / (2.0 * trials)) / denom
    margin = (z * math.sqrt(phat * (1.0 - phat) / trials + z * z / (4.0 * trials * trials))) / denom
    return {
        "estimate": round(phat, 6),
        "lower": round(max(0.0, center - margin), 6),
        "upper": round(min(1.0, center + margin), 6),
        "trials": trials,
        "method": "wilson_score",
    }


def cumulative_grid(intervals: Sequence[tuple[float, float]], grid_days: Sequence[int],
                    horizon_days: int, at_risk: int | None = None) -> list[dict[str, Any]]:
    """区间删失事件在统一网格上的累计发生比例（上下界分列，不做中点插补）。

    事件已知发生在 (lower, upper] 天之间。对网格点 t：

    * upper <= t 的个体事件必然已发生，计入 ``occurred``；
    * lower >= t 的个体事件必然未发生；
    * 其余个体是否已发生不可辨认，计入 ``indeterminate``，其人数给出
      累计比例的不确定带 [occurred/n, (occurred+indeterminate)/n]。

    ``at_risk`` 为届次入组总人数；缺省取事件样本数。分母固定为入组人数，
    未发生事件（含退出、失联）的个体同样留在风险集中，不被删除。
    """
    n = at_risk if at_risk is not None else len(intervals)
    rows = []
    for day in grid_days:
        if day > horizon_days:
            continue
        occurred = sum(1 for lower, upper in intervals if upper <= day)
        indeterminate = sum(1 for lower, upper in intervals if lower < day < upper)
        rows.append({
            "day": day,
            "at_risk": n,
            "occurred": occurred,
            "indeterminate": indeterminate,
            "cumulative_lower": round(occurred / n, 6) if n else None,
            "cumulative_upper": round((occurred + indeterminate) / n, 6) if n else None,
            "point_estimate": wilson_interval(occurred, n)["estimate"] if n else None,
        })
    return rows


def direct_standardization(stratum_rates: dict[str, float | None],
                           weights: dict[str, float]) -> dict[str, Any]:
    """按给定层权重做直接标准化；缺失层的权重重分配到可用层。"""
    available = {key: rate for key, rate in stratum_rates.items() if rate is not None}
    usable_weight = sum(weights.get(key, 0.0) for key in available)
    if usable_weight <= 0.0:
        return {"standardized_rate": None, "covered_weight": 0.0, "missing_strata": sorted(stratum_rates)}
    standardized = sum(
        rate * weights[key] / usable_weight for key, rate in available.items()
    )
    missing = sorted(key for key, rate in stratum_rates.items() if rate is None)
    return {
        "standardized_rate": round(standardized, 6),
        "covered_weight": round(usable_weight, 6),
        "missing_strata": missing,
    }


def bootstrap_paired_difference(samples: Sequence[tuple[bool, bool]],
                                replicates: int = BOOTSTRAP_REPLICATES,
                                seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """对配对 0/1 样本（同一商户在旧/新规则下是否升级）做 bootstrap 差值区间。

    返回旧规则率、新规则率、配对差值（旧-新）及其百分位置信区间。
    """
    n = len(samples)
    if n == 0:
        return {"n": 0, "old_rate": None, "new_rate": None, "difference": None,
                "lower": None, "upper": None, "replicates": replicates, "method": "percentile_bootstrap"}
    rng = random.Random(seed)
    old_rate = sum(1 for old, _ in samples if old) / n
    new_rate = sum(1 for _, new in samples if new) / n
    differences = []
    for _ in range(replicates):
        old_hits = 0
        new_hits = 0
        for _index in range(n):
            old, new = samples[rng.randrange(n)]
            old_hits += int(old)
            new_hits += int(new)
        differences.append((old_hits - new_hits) / n)
    ordered = sorted(differences)
    tail = (1.0 - CONFIDENCE_LEVEL) / 2.0
    lower = ordered[max(0, int(tail * replicates) - 1 + 1)]
    upper = ordered[min(replicates - 1, int((1.0 - tail) * replicates))]
    return {
        "n": n,
        "old_rate": round(old_rate, 6),
        "new_rate": round(new_rate, 6),
        "difference": round(old_rate - new_rate, 6),
        "lower": round(lower, 6),
        "upper": round(upper, 6),
        "replicates": replicates,
        "method": "percentile_bootstrap",
    }


def pooled_weights(records: Sequence[Any], stratum_fn: Callable[[Any], str]) -> dict[str, float]:
    """以合并样本的层分布作为标准化参考权重。"""
    counts: dict[str, int] = {}
    for record in records:
        key = stratum_fn(record)
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {}
    return {key: count / total for key, count in counts.items()}


def total_variation_distance(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    return round(0.5 * sum(abs(left.get(key, 0.0) - right.get(key, 0.0)) for key in keys), 6)
