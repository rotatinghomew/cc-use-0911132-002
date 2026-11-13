"""迁移研究编排：输入指纹、study_id、幂等重放与结果组装。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from brand_ladder_observatory.foundation.registry import DomainError, fingerprint, stable_json
from brand_ladder_observatory.transition_study.engine import analyze
from brand_ladder_observatory.transition_study.model import parse_input
from brand_ladder_observatory.transition_study.persistence import TransitionStudyStore

REQUEST_KEY_PREFIX = "transition-study:"


def _canonical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """去掉与分析无关的易变包装，保证同一业务输入指纹稳定。"""
    canonical = dict(payload)
    return canonical


def run_study(input_path: str | Path, database: str | Path,
              recency_tolerance_days: int = 30) -> dict[str, Any]:
    """读取输入、执行迁移研究并写入 registry；同输入重放返回同一 study_id。"""
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise DomainError("分析输入必须是 JSON 对象")
    input_fingerprint = fingerprint(stable_json(_canonical_payload(raw)))
    request_key = REQUEST_KEY_PREFIX + input_fingerprint

    store = TransitionStudyStore(database)
    recalled = store.recall(request_key)
    if recalled is not None:
        recalled["replayed"] = True
        return recalled

    study, quality = parse_input(raw, recency_tolerance_days=recency_tolerance_days)
    analyzed = analyze(study, quality)
    merchant_paths = analyzed.pop("_merchant_paths")

    study_id = "ts-" + input_fingerprint[:16]
    baseline_linked = store.baseline_case_exists(study.baseline_case_id)
    result = {
        "study_id": study_id,
        "replayed": False,
        "as_of": study.as_of.isoformat(),
        "horizon_days": study.horizon_days,
        "recency_tolerance_days": study.recency_tolerance_days,
        "cohorts": analyzed["cohorts"],
        "transitions": analyzed["transitions"],
        "uncertainty": analyzed["uncertainty"],
        "comparability": analyzed["comparability"],
        "versions": analyzed["versions"],
        "cross_cohort_structure_tvd": analyzed["cross_cohort_structure_tvd"],
        "cross_cohort_comparability": analyzed["cross_cohort_comparability"],
        "data_quality": analyzed["data_quality"],
        "registry": {
            "baseline_case_id": study.baseline_case_id,
            "baseline_case_linked": baseline_linked,
            "study_records_in_registry": store.study_count() + 1,
        },
        "method_summary": [
            "以入驻届次为分析单位，时间原点为 entered_at，统一观察 horizon_days 天",
            "每届次分别在参考规则与备选规则下从 features 重建完整状态路径，规则间不拼接",
            "相邻走访构成区间删失转移对；多档跳变保留不可辨认标记，不插补中间状态",
            "端点互斥划分为首次升级、先升后落、仍在观察（无变档）、退出、无法辨认",
            "比例给 Wilson 95% 区间；规则差值给固定种子配对 bootstrap 百分位区间",
            "失联不计注销、入组前不填充、晚于 as_of 可用的事实不采用",
        ],
    }
    store.save(
        study_id=study_id,
        request_key=request_key,
        input_fingerprint=input_fingerprint,
        baseline_case_id=study.baseline_case_id,
        as_of=study.as_of.isoformat(),
        horizon_days=study.horizon_days,
        reference_rule=study.reference_rule,
        alternate_rule=study.alternate_rule,
        result=result,
        merchant_paths=merchant_paths,
    )
    return result
