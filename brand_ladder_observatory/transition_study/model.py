"""输入解析、规则重建与商户状态路径模型。

设计约束（与 fixtures/analysis/fields.md 口径一致）：

* 失联不等于注销：没有后续 visit 不能推断为退出，只有 exits 中确认的注册
  退出才计为退出；
* 首次进入视野之前的经历未知，不允许向前填充状态；
* 两次观测跨越多个档位时，只能记一次区间删失的多档跳变，不能虚构中间
  转移；
* 不同 rule_version 的 state 不得直接拼接成一条历史；每条规则下的路径都
  必须用统一规则从 features 重新分类得到；
* available_at 晚于分析截止 as_of 的事实尚不可采用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from brand_ladder_observatory.foundation.registry import DomainError, get_path

# 品牌梯队由低到高的固定次序；unbranded 为规则可能输出的最低档，本批资料未出现。
TIER_ORDER = ("unbranded", "oem", "independent", "regional")
TIER_RANK = {name: index for index, name in enumerate(TIER_ORDER)}
RULE_VERSIONS = ("old", "new")


def parse_instant(value: str) -> datetime:
    """解析带时区 ISO 8601 时刻；缺失时区按 UTC 处理并原样保留信息。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Rule:
    """一套品牌梯队分类规则。

    own_brand_sales >= regional_threshold 判 regional，否则
    >= independent_threshold 判 independent，再否则判 oem。
    """

    version: str
    independent_threshold: float
    regional_threshold: float

    def classify(self, features: Mapping[str, Any]) -> str:
        sales = float(features.get("own_brand_sales", 0.0))
        if sales >= self.regional_threshold:
            return "regional"
        if sales >= self.independent_threshold:
            return "independent"
        return "oem"


@dataclass(slots=True)
class Visit:
    visit_id: str
    merchant_id: str
    observed_at: datetime
    available_at: datetime
    recorded_state: str
    rule_version: str
    features: dict[str, Any]

    def state_under(self, rule: Rule) -> str:
        return rule.classify(self.features)


@dataclass(slots=True)
class ExitRecord:
    merchant_id: str
    exited_at: datetime
    available_at: datetime
    reason: str


@dataclass(slots=True)
class Merchant:
    merchant_id: str
    entered_at: datetime
    entry_known_at: datetime
    cohort: str
    sector: str
    visits: list[Visit] = field(default_factory=list)
    exit_record: ExitRecord | None = None


@dataclass(slots=True)
class StudyInput:
    schema_version: int
    as_of: datetime
    source_versions: dict[str, str]
    rules: dict[str, Rule]
    merchants: dict[str, Merchant]
    horizon_days: int
    reference_rule: str
    alternate_rule: str
    baseline_case_id: str | None
    recency_tolerance_days: int
    raw: dict[str, Any]


@dataclass(slots=True)
class DataQuality:
    """输入核对结果；硬错误直接抛出，软问题保留计数随结果展示。"""

    orphan_visits: list[str] = field(default_factory=list)
    orphan_exits: list[str] = field(default_factory=list)
    pre_entry_visits: list[str] = field(default_factory=list)
    late_visits: list[str] = field(default_factory=list)
    late_exits: list[str] = field(default_factory=list)
    duplicate_visits: list[str] = field(default_factory=list)
    state_mismatches: list[str] = field(default_factory=list)
    missing_feature_visits: list[str] = field(default_factory=list)
    merchants_without_visits: list[str] = field(default_factory=list)
    native_version_mixes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "orphan_visits": self.orphan_visits,
            "orphan_exits": self.orphan_exits,
            "pre_entry_visits_ignored": self.pre_entry_visits,
            "late_visits_ignored": self.late_visits,
            "late_exits_ignored": self.late_exits,
            "duplicate_visit_ids": self.duplicate_visits,
            "recorded_state_mismatches": self.state_mismatches,
            "missing_feature_visits": self.missing_feature_visits,
            "merchants_without_visits": self.merchants_without_visits,
            "merchants_mixing_native_rule_versions": self.native_version_mixes,
        }


def _require(payload: Mapping[str, Any], path: str) -> Any:
    value = get_path(payload, path)
    if value is None:
        raise DomainError(f"输入缺少必填字段：{path}")
    return value


def parse_input(payload: Mapping[str, Any], recency_tolerance_days: int = 30) -> tuple[StudyInput, DataQuality]:
    """把原始 JSON 输入解析成强类型研究输入，并完成数据质量核对。"""
    quality = DataQuality()
    as_of = parse_instant(str(_require(payload, "as_of")))

    rules: dict[str, Rule] = {}
    for item in _require(payload, "rules"):
        version = str(item["version"])
        rules[version] = Rule(
            version=version,
            independent_threshold=float(item["independent_threshold"]),
            regional_threshold=float(item["regional_threshold"]),
        )
    study = _require(payload, "study")
    reference_rule = str(study["reference_rule"])
    alternate_rule = str(study["alternate_rule"])
    for version in (reference_rule, alternate_rule):
        if version not in rules:
            raise DomainError(f"研究引用的规则版本不存在：{version}")

    merchants: dict[str, Merchant] = {}
    for item in _require(payload, "merchants"):
        merchant_id = str(item["merchant_id"])
        if merchant_id in merchants:
            raise DomainError(f"merchant_id 重复：{merchant_id}")
        merchants[merchant_id] = Merchant(
            merchant_id=merchant_id,
            entered_at=parse_instant(str(item["entered_at"])),
            entry_known_at=parse_instant(str(item["entry_known_at"])),
            cohort=str(item["cohort"]),
            sector=str(item.get("sector", "unknown")),
        )

    seen_visit_ids: set[str] = set()
    native_versions: dict[str, set[str]] = {}
    for item in _require(payload, "visits"):
        visit_id = str(item["visit_id"])
        if visit_id in seen_visit_ids:
            quality.duplicate_visits.append(visit_id)
            continue
        seen_visit_ids.add(visit_id)
        merchant_id = str(item["merchant_id"])
        merchant = merchants.get(merchant_id)
        if merchant is None:
            quality.orphan_visits.append(visit_id)
            continue
        visit = Visit(
            visit_id=visit_id,
            merchant_id=merchant_id,
            observed_at=parse_instant(str(item["observed_at"])),
            available_at=parse_instant(str(item["available_at"])),
            recorded_state=str(item["state"]),
            rule_version=str(item["rule_version"]),
            features=dict(item.get("features") or {}),
        )
        if visit.rule_version not in rules:
            raise DomainError(f"走访 {visit_id} 引用未知规则版本：{visit.rule_version}")
        if "own_brand_sales" not in visit.features:
            quality.missing_feature_visits.append(visit_id)
        else:
            rebuilt = visit.state_under(rules[visit.rule_version])
            if rebuilt != visit.recorded_state:
                quality.state_mismatches.append(
                    f"{visit_id}: recorded={visit.recorded_state}, rebuilt={rebuilt}"
                )
        native_versions.setdefault(merchant_id, set()).add(visit.rule_version)
        if visit.observed_at < merchant.entered_at:
            # 入组之前没有状态记录，不允许向前填充：该次走访不进入任何路径。
            quality.pre_entry_visits.append(visit_id)
            continue
        if visit.available_at > as_of:
            # 晚到事实在截止时刻尚不可采用。
            quality.late_visits.append(visit_id)
            continue
        merchant.visits.append(visit)

    for item in payload.get("exits", []):
        merchant_id = str(item["merchant_id"])
        merchant = merchants.get(merchant_id)
        if merchant is None:
            quality.orphan_exits.append(merchant_id)
            continue
        exit_record = ExitRecord(
            merchant_id=merchant_id,
            exited_at=parse_instant(str(item["exited_at"])),
            available_at=parse_instant(str(item["available_at"])),
            reason=str(item.get("reason", "unknown")),
        )
        if exit_record.available_at > as_of:
            quality.late_exits.append(merchant_id)
            continue
        merchant.exit_record = exit_record

    for merchant in merchants.values():
        merchant.visits.sort(key=lambda visit: (visit.observed_at, visit.visit_id))
        if not merchant.visits:
            quality.merchants_without_visits.append(merchant.merchant_id)
        if len(native_versions.get(merchant.merchant_id, set())) > 1:
            quality.native_version_mixes.append(merchant.merchant_id)

    parsed = StudyInput(
        schema_version=int(payload.get("schema_version", 1)),
        as_of=as_of,
        source_versions=dict(payload.get("source_versions") or {}),
        rules=rules,
        merchants=merchants,
        horizon_days=int(study["horizon_days"]),
        reference_rule=reference_rule,
        alternate_rule=alternate_rule,
        baseline_case_id=payload.get("baseline_case_id"),
        recency_tolerance_days=int(recency_tolerance_days),
        raw=dict(payload),
    )
    return parsed, quality


@dataclass(slots=True)
class StatePoint:
    """单次走访在某一套规则下重述后的状态点。"""

    visit_id: str
    observed_at: datetime
    state: str
    native_rule_version: str
    rebuilt_from_features: bool


@dataclass(slots=True)
class MerchantPath:
    """单个商户在单一规则版本下的完整可辨识信息。"""

    merchant_id: str
    cohort: str
    sector: str
    rule_version: str
    origin: datetime
    censor_time: datetime
    horizon_days: int
    points: list[StatePoint]
    exit_at: datetime | None
    exit_reason: str | None
    endpoint_label: str
    endpoint_detail: str
    ever_upgraded: bool
    ever_fell_back: bool
    first_upgrade_interval: tuple[float, float] | None
    first_fallback_interval: tuple[float, float] | None
    multistep_pairs: list[dict[str, Any]]
    mixed_native_versions: bool
    assessable: bool
    note: str


def build_path(
    merchant: Merchant,
    rule: Rule,
    censor_time: datetime,
    horizon_days: int,
    recency_tolerance_days: int,
    mixed_native_versions: bool,
) -> MerchantPath:
    """按统一规则重建商户路径，并在统一观察窗内做端点归类。

    端点互斥归类（assessable=True 时）：

    * ``exited``：窗内确认注册退出（退出是竞争事件，保留退出前已观测的升级）；
    * ``first_upgrade``：未退出、窗内至少一次向更高档位移动且未出现回落；
    * ``upgrade_then_fallback``：未退出、窗内先升级后出现向更低档位移动；
    * ``no_change_observed``：未退出、窗内有足够新近观测且状态从未变档；
    * ``unidentifiable``：失联（末次走访早于窗末宽限期）或入组后无有效
      走访，无法辨认窗末状态。
    """
    points: list[StatePoint] = []
    for visit in merchant.visits:
        if visit.observed_at > censor_time:
            break
        points.append(
            StatePoint(
                visit_id=visit.visit_id,
                observed_at=visit.observed_at,
                state=visit.state_under(rule),
                native_rule_version=visit.rule_version,
                rebuilt_from_features="own_brand_sales" in visit.features,
            )
        )

    exit_at = None
    exit_reason = None
    if merchant.exit_record is not None and merchant.exit_record.exited_at <= censor_time:
        exit_at = merchant.exit_record.exited_at
        exit_reason = merchant.exit_record.reason

    ever_upgraded = False
    ever_fell_back = False
    first_upgrade_interval: tuple[float, float] | None = None
    first_fallback_interval: tuple[float, float] | None = None
    multistep_pairs: list[dict[str, Any]] = []

    for earlier, later in zip(points, points[1:]):
        rank_before = TIER_RANK[earlier.state]
        rank_after = TIER_RANK[later.state]
        gap_days = (later.observed_at - earlier.observed_at).total_seconds() / 86400.0
        elapsed_before = (earlier.observed_at - merchant.entered_at).total_seconds() / 86400.0
        elapsed_after = (later.observed_at - merchant.entered_at).total_seconds() / 86400.0
        if rank_after > rank_before:
            ever_upgraded = True
            if first_upgrade_interval is None:
                # 区间删失：升级只知道发生在两次走访之间，不做中点插补。
                first_upgrade_interval = (max(0.0, elapsed_before), elapsed_after)
            if rank_after - rank_before > 1:
                multistep_pairs.append(
                    {
                        "from_visit": earlier.visit_id,
                        "to_visit": later.visit_id,
                        "from_state": earlier.state,
                        "to_state": later.state,
                        "gap_days": round(gap_days, 3),
                        "intermediate_states": "not_observable",
                    }
                )
        elif rank_after < rank_before:
            ever_fell_back = True
            if first_fallback_interval is None:
                first_fallback_interval = (max(0.0, elapsed_before), elapsed_after)
            if rank_before - rank_after > 1:
                multistep_pairs.append(
                    {
                        "from_visit": earlier.visit_id,
                        "to_visit": later.visit_id,
                        "from_state": earlier.state,
                        "to_state": later.state,
                        "gap_days": round(gap_days, 3),
                        "intermediate_states": "not_observable",
                    }
                )

    note = ""
    if exit_at is not None:
        endpoint_label = "exited"
        endpoint_detail = "registry_exit_within_horizon"
        assessable = True
    elif not points:
        endpoint_label = "unidentifiable"
        endpoint_detail = "no_observation_after_entry"
        assessable = False
        note = "首次进入视野前的经历未知，未做任何状态填充"
    else:
        last_observed = points[-1].observed_at
        fresh_enough = (censor_time - last_observed).total_seconds() / 86400.0 <= recency_tolerance_days
        if not fresh_enough:
            endpoint_label = "unidentifiable"
            endpoint_detail = "lost_contact_within_horizon"
            assessable = False
            note = "失联不等于注销：无确认退出且末次走访早于窗末宽限期"
        elif ever_fell_back:
            endpoint_label = "upgrade_then_fallback"
            endpoint_detail = "upgraded_and_later_moved_to_lower_tier"
            assessable = True
        elif ever_upgraded:
            endpoint_label = "first_upgrade"
            endpoint_detail = "upgraded_within_horizon_no_fallback_observed"
            assessable = True
        else:
            endpoint_label = "no_change_observed"
            endpoint_detail = "observed_without_tier_change"
            assessable = True

    return MerchantPath(
        merchant_id=merchant.merchant_id,
        cohort=merchant.cohort,
        sector=merchant.sector,
        rule_version=rule.version,
        origin=merchant.entered_at,
        censor_time=censor_time,
        horizon_days=horizon_days,
        points=points,
        exit_at=exit_at,
        exit_reason=exit_reason,
        endpoint_label=endpoint_label,
        endpoint_detail=endpoint_detail,
        ever_upgraded=ever_upgraded,
        ever_fell_back=ever_fell_back,
        first_upgrade_interval=first_upgrade_interval,
        first_fallback_interval=first_fallback_interval,
        multistep_pairs=multistep_pairs,
        mixed_native_versions=mixed_native_versions,
        assessable=assessable,
        note=note,
    )
