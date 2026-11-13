"""迁移研究方法正确性测试。

覆盖业务说明中的反例：失联不等于注销、入组前不填充、多档跳变不虚构中间
转移、晚到事实不采用、规则切换必须平行重建、跨届次结构差异、小样本标记、
幂等重放与既有 registry 共存。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brand_ladder_observatory.service import ApplicationService
from brand_ladder_observatory.transition_study.model import (
    TIER_RANK,
    Rule,
    parse_input,
    parse_instant,
)
from brand_ladder_observatory.transition_study.stats import (
    bootstrap_paired_difference,
    cumulative_grid,
    direct_standardization,
    wilson_interval,
)
from brand_ladder_observatory.transition_study.study import run_study

UTC = timezone.utc
HORIZON = 60
AS_OF = "2026-12-15T00:00:00+00:00"


def instant(base: datetime, days: float) -> str:
    return (base + timedelta(days=days)).isoformat()


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.old = Rule("old", 30, 60)
        self.new = Rule("new", 40, 70)

    def test_threshold_boundaries(self):
        self.assertEqual(self.old.classify({"own_brand_sales": 29}), "oem")
        self.assertEqual(self.old.classify({"own_brand_sales": 30}), "independent")
        self.assertEqual(self.old.classify({"own_brand_sales": 59}), "independent")
        self.assertEqual(self.old.classify({"own_brand_sales": 60}), "regional")
        # 同一特征在新旧规则下可以落不同档位。
        self.assertEqual(self.new.classify({"own_brand_sales": 35}), "oem")
        self.assertEqual(self.old.classify({"own_brand_sales": 35}), "independent")
        self.assertEqual(self.new.classify({"own_brand_sales": 65}), "independent")
        self.assertEqual(self.old.classify({"own_brand_sales": 65}), "regional")


class ScenarioBuilder:
    """按场景构造最小输入；默认时间原点 2026-09-01，as_of 在其后 105 天。"""

    def __init__(self):
        self.merchants: list[dict] = []
        self.visits: list[dict] = []
        self.exits: list[dict] = []
        self._seq = 0

    def merchant(self, merchant_id: str, cohort: str = "c1", sector: str = "leather",
                 entry_day: int = 0) -> str:
        base = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=entry_day)
        self.merchants.append({
            "merchant_id": merchant_id,
            "entered_at": base.isoformat(),
            "entry_known_at": base.isoformat(),
            "cohort": cohort,
            "sector": sector,
        })
        return merchant_id

    def visit(self, merchant_id: str, day: float, sales: float, native_rule: str = "old",
              state: str | None = None, available_day: float | None = None,
              visit_id: str | None = None) -> None:
        self._seq += 1
        if any(item["merchant_id"] == merchant_id for item in self.merchants):
            base = self._entry(merchant_id)
            observed = base + timedelta(days=day)
            available = observed + timedelta(days=1) if available_day is None else \
                base + timedelta(days=available_day)
        else:
            # 孤儿走访：使用固定参考时间，仅用于验证输入核对。
            reference = datetime(2026, 9, 1, tzinfo=UTC)
            observed = reference + timedelta(days=day)
            available = observed + timedelta(days=1)
        self.visits.append({
            "visit_id": visit_id or f"v{self._seq}",
            "merchant_id": merchant_id,
            "observed_at": observed.isoformat(),
            "available_at": available.isoformat(),
            "state": state or self._state(sales, native_rule),
            "rule_version": native_rule,
            "features": {"own_brand_sales": sales},
        })

    def exit(self, merchant_id: str, day: float, available_day: float | None = None,
             reason: str = "registry_closed") -> None:
        if any(item["merchant_id"] == merchant_id for item in self.merchants):
            base = self._entry(merchant_id)
        else:
            base = datetime(2026, 9, 1, tzinfo=UTC)
        exited = base + timedelta(days=day)
        available = exited + timedelta(days=2) if available_day is None else \
            base + timedelta(days=available_day)
        self.exits.append({
            "merchant_id": merchant_id,
            "exited_at": exited.isoformat(),
            "available_at": available.isoformat(),
            "reason": reason,
        })

    def _entry(self, merchant_id: str) -> datetime:
        for item in self.merchants:
            if item["merchant_id"] == merchant_id:
                return parse_instant(item["entered_at"])
        raise KeyError(merchant_id)

    @staticmethod
    def _state(sales: float, rule: str) -> str:
        if rule == "old":
            return "regional" if sales >= 60 else "independent" if sales >= 30 else "oem"
        return "regional" if sales >= 70 else "independent" if sales >= 40 else "oem"

    def build(self) -> dict:
        return {
            "schema_version": 1,
            "as_of": AS_OF,
            "source_versions": {"operations": "t1", "reference": "t1"},
            "merchants": self.merchants,
            "visits": self.visits,
            "exits": self.exits,
            "rules": [
                {"version": "old", "independent_threshold": 30, "regional_threshold": 60},
                {"version": "new", "independent_threshold": 40, "regional_threshold": 70},
            ],
            "study": {"horizon_days": HORIZON, "reference_rule": "old", "alternate_rule": "new"},
            "baseline_case_id": "brand_ladder_observatory-demo",
        }


class ParsingTests(unittest.TestCase):
    def test_late_visits_and_exits_are_ignored(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20, available_day=200)  # as_of 之后才可用
        builder.exit("m1", 10, available_day=200)
        study, quality = parse_input(builder.build())
        self.assertEqual(quality.late_visits, ["v1"])
        self.assertEqual(quality.late_exits, ["m1"])
        self.assertEqual(study.merchants["m1"].visits, [])
        self.assertIsNone(study.merchants["m1"].exit_record)

    def test_pre_entry_visit_never_backfilled(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", -3, 20)
        builder.visit("m1", 10, 35)
        study, quality = parse_input(builder.build())
        self.assertEqual(quality.pre_entry_visits, ["v1"])
        self.assertEqual([v.visit_id for v in study.merchants["m1"].visits], ["v2"])

    def test_orphan_records_and_duplicates(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20)                                   # v1
        builder.visit("ghost", 5, 20)                                # v2 孤儿
        builder.visit("m1", 6, 22, visit_id="v1")                    # 重复 visit_id
        builder.exit("ghost-exit", 7)
        _, quality = parse_input(builder.build())
        self.assertEqual(quality.orphan_visits, ["v2"])
        self.assertEqual(quality.orphan_exits, ["ghost-exit"])
        self.assertEqual(quality.duplicate_visits, ["v1"])

    def test_recorded_state_mismatch_detected(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20, state="regional")  # 20 在 old 规则下应为 oem
        _, quality = parse_input(builder.build())
        self.assertEqual(len(quality.state_mismatches), 1)


class PathAndEndpointTests(unittest.TestCase):
    def _run(self, builder: ScenarioBuilder) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            ApplicationService(db)  # 建表即可
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(builder.build()), encoding="utf-8")
            return run_study(input_path, db)

    def _paths(self, result: dict, rule: str) -> dict[str, dict]:
        # 通过输出端点反查；细粒度路径在 transitions 的端点划分中体现。
        block = next(item for item in result["transitions"] if item["rule_version"] == rule)
        return {row["label"]: row for row in block["endpoint_partition"]["partition"]}

    def test_lost_contact_is_not_exit(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        # 末次走访在第 5 天，窗末第 60 天，远超 30 天宽限期；无退出登记。
        builder.visit("m1", 2, 20)
        builder.visit("m1", 5, 22)
        result = self._run(builder)
        partition = self._paths(result, "old")
        self.assertEqual(partition["unidentifiable"]["count"], 1)
        self.assertEqual(partition["exited"]["count"], 0)
        cohort = result["cohorts"][0]
        self.assertFalse(cohort["comparable_without_caution"])

    def test_confirmed_exit_is_competing_event_keeping_prior_upgrade(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20)
        builder.visit("m1", 20, 35)   # oem -> independent，升级
        builder.exit("m1", 40)        # 窗内确认退出
        result = self._run(builder)
        partition = self._paths(result, "old")
        self.assertEqual(partition["exited"]["count"], 1)
        block = next(item for item in result["transitions"] if item["rule_version"] == "old")
        # 退出前的升级仍被保留为升级事件。
        self.assertEqual(block["time_to_first_upgrade"]["upgraders"], 1)
        exit_grid = block["cumulative_incidence"]["exit"]
        self.assertEqual(exit_grid[-1]["occurred"], 1)
        # 分母固定为入组总人数，退出/失联个体不被从风险集删除。
        self.assertEqual(exit_grid[-1]["at_risk"], 1)
        for kind in ("first_upgrade", "first_fallback", "exit"):
            self.assertTrue(
                all(row["at_risk"] == 1 for row in block["cumulative_incidence"][kind])
            )

    def test_multistep_jump_keeps_indeterminate_middle(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20)    # oem
        builder.visit("m1", 25, 65)   # 直接 regional，跨过 independent
        result = self._run(builder)
        block = next(item for item in result["transitions"] if item["rule_version"] == "old")
        self.assertEqual(block["pair_transitions"]["indeterminate_pair_count"], 1)
        jump = block["pair_transitions"]["multistep_jump_pairs"]
        self.assertEqual(jump[0]["from_state"], "oem")
        self.assertEqual(jump[0]["to_state"], "regional")
        self.assertEqual(jump[0]["intermediate_states"], "not_observable")
        # 仍计一次升级，但没有任何 oem->independent 或 independent->regional 的虚构对。
        self.assertEqual(block["time_to_first_upgrade"]["upgraders"], 1)
        matrix_pairs = {(r["from_state"], r["to_state"]): r["count"]
                        for r in block["pair_transitions"]["matrix"]}
        self.assertEqual(matrix_pairs.get(("oem", "independent"), 0), 0)

    def test_rule_switch_rebuilds_separate_paths_no_fake_trajectory(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        # 35 在 old 下是 independent，在 new 下仍是 oem；25 两规则都是 oem。
        builder.visit("m1", 5, 25, native_rule="old")
        builder.visit("m1", 20, 35, native_rule="new")
        result = self._run(builder)
        old_block = next(item for item in result["transitions"] if item["rule_version"] == "old")
        new_block = next(item for item in result["transitions"] if item["rule_version"] == "new")
        # old 重建路径：oem -> independent（升级）；new 重建路径：oem -> oem（无变化）。
        self.assertEqual(old_block["time_to_first_upgrade"]["upgraders"], 1)
        self.assertEqual(new_block["time_to_first_upgrade"]["upgraders"], 0)
        comparison = result["comparability"][0]
        self.assertEqual(comparison["discordant_count"], 1)
        self.assertFalse(comparison["comparable"])
        self.assertIn("features", comparison["reconstruction"])

    def test_late_entry_cohort_uses_own_origin_and_censor(self):
        builder = ScenarioBuilder()
        builder.merchant("early", cohort="c1", entry_day=0)
        builder.visit("early", 10, 20)
        builder.visit("early", 20, 35)
        builder.merchant("late", cohort="c1", entry_day=40)  # 迟入组 40 天
        builder.visit("late", 10, 20)
        builder.visit("late", 20, 35)
        result = self._run(builder)
        block = next(item for item in result["transitions"] if item["rule_version"] == "old")
        # 两个商户各自从 entered_at 起算 60 天，升级都在自己的窗内。
        self.assertEqual(block["time_to_first_upgrade"]["upgraders"], 2)
        self.assertEqual(block["endpoint_partition"]["enrolled"], 2)

    def test_no_observation_after_entry_is_unidentifiable(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")  # 完全无走访
        result = self._run(builder)
        partition = self._paths(result, "old")
        self.assertEqual(partition["unidentifiable"]["count"], 1)
        self.assertEqual(result["data_quality"]["merchants_without_visits"], ["m1"])


class GridAndIntervalTests(unittest.TestCase):
    def test_cumulative_grid_bands_with_full_cohort_denominator(self):
        # 4 人入组，2 人已知在 (10,20] 升级，网格点 15 时这 2 人不可辨认。
        intervals = [(10.0, 20.0), (10.0, 20.0)]
        rows = cumulative_grid(intervals, [15, 20, 30], 30, at_risk=4)
        at15 = next(row for row in rows if row["day"] == 15)
        self.assertEqual(at15["occurred"], 0)
        self.assertEqual(at15["indeterminate"], 2)
        self.assertEqual(at15["cumulative_lower"], 0.0)
        self.assertEqual(at15["cumulative_upper"], 0.5)
        at20 = next(row for row in rows if row["day"] == 20)
        self.assertEqual(at20["occurred"], 2)
        self.assertEqual(at20["cumulative_lower"], 0.5)

    def test_wilson_interval_handles_extremes_and_empty(self):
        zero = wilson_interval(0, 10)
        self.assertEqual(zero["lower"], 0.0)
        self.assertGreater(zero["upper"], 0.0)
        one = wilson_interval(10, 10)
        self.assertLess(one["lower"], 1.0)
        self.assertEqual(one["upper"], 1.0)
        empty = wilson_interval(0, 0)
        self.assertIsNone(empty["estimate"])

    def test_bootstrap_difference_is_seeded_and_paired(self):
        samples = [(True, False), (True, True), (False, False), (False, True)]
        first = bootstrap_paired_difference(samples, seed=7)
        second = bootstrap_paired_difference(samples, seed=7)
        self.assertEqual(first["lower"], second["lower"])
        self.assertEqual(first["upper"], second["upper"])
        self.assertEqual(first["old_rate"], 0.5)
        self.assertEqual(first["new_rate"], 0.5)
        self.assertEqual(first["difference"], 0.0)

    def test_direct_standardization_reports_missing_strata(self):
        weights = {"a": 0.75, "b": 0.25}
        result = direct_standardization({"a": 0.4, "b": None}, weights)
        self.assertAlmostEqual(result["standardized_rate"], 0.4)
        self.assertEqual(result["covered_weight"], 0.75)
        self.assertEqual(result["missing_strata"], ["b"])


class CohortStructureTests(unittest.TestCase):
    def test_structurally_different_cohorts_flagged(self):
        builder = ScenarioBuilder()
        # c1 全部 oem 入组；c2 全部 independent 入组。
        for index in range(6):
            mid = f"a{index}"
            builder.merchant(mid, cohort="c1", sector="leather")
            builder.visit(mid, 5, 20)
            builder.visit(mid, 25, 35)
        for index in range(6):
            mid = f"b{index}"
            builder.merchant(mid, cohort="c2", sector="leather")
            builder.visit(mid, 5, 35)
            builder.visit(mid, 25, 50)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            ApplicationService(db)
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(builder.build()), encoding="utf-8")
            result = run_study(input_path, db)
        self.assertEqual({item["cohort"] for item in result["cohorts"]}, {"c1", "c2"})
        tvd = result["cross_cohort_structure_tvd"]["c1_vs_c2"]
        self.assertEqual(tvd, 1.0)
        pair = result["cross_cohort_comparability"]["pairwise"][0]
        self.assertTrue(pair["structurally_different"])
        self.assertIn("标准化", pair["recommendation"])
        # 每届次各自输出端点划分，不存在跨届次混合的单一总升级率结论。
        self.assertEqual(len(result["transitions"]), 4)


class RegistryIntegrationTests(unittest.TestCase):
    def test_replay_returns_same_study_id_and_coexists_with_runs(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20)
        builder.visit("m1", 25, 35)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            service = ApplicationService(db)
            service.execute({"case_id": "brand_ladder_observatory-demo"})
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(builder.build()), encoding="utf-8")
            first = run_study(input_path, db)
            replay = run_study(input_path, db)
            self.assertFalse(first["replayed"])
            self.assertTrue(replay["replayed"])
            self.assertEqual(first["study_id"], replay["study_id"])
            self.assertTrue(first["registry"]["baseline_case_linked"])
            # 既有逐案运行与新研究表共存。
            stats = service.ledger.stats()
            self.assertGreaterEqual(stats["runs"], 1)
            # 静态分类与商户查询产物仍然完整；同输入第二次执行为幂等重放。
            query = service.execute({"case_id": "brand_ladder_observatory-demo"})
            self.assertIn("ladder_classification", query["artifacts"])
            self.assertTrue(query["replayed"])

    def test_runs_standalone_on_fresh_registry_without_runs_table(self):
        builder = ScenarioBuilder()
        builder.merchant("m1")
        builder.visit("m1", 5, 20)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fresh.sqlite3"  # 不经 ApplicationService，无 runs 表
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(builder.build()), encoding="utf-8")
            result = run_study(input_path, db)
            self.assertFalse(result["registry"]["baseline_case_linked"])
            self.assertEqual(result["study_id"], run_study(input_path, db)["study_id"])

    def test_fixture_study_runs_and_is_deterministic(self):
        fixture = Path(__file__).resolve().parents[2] / "fixtures/analysis/input.json"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            ApplicationService(db)
            first = run_study(fixture, db)
            second = run_study(fixture, db)
        required = ["study_id", "cohorts", "transitions", "uncertainty",
                    "comparability", "versions"]
        for key in required:
            self.assertIn(key, first)
        self.assertEqual(first["study_id"], second["study_id"])
        # 规则重建与记录状态零分歧。
        self.assertEqual(first["versions"]["state_rebuild_check"]["mismatch_count"], 0)
        # 所有累计网格的分母均为届次入组总人数（28），不是事件人数。
        for block in first["transitions"]:
            for kind in ("first_upgrade", "first_fallback", "exit"):
                self.assertTrue(
                    all(row["at_risk"] == 28 for row in block["cumulative_incidence"][kind])
                )
        # 旧规则下 merchant-2 是先升后落，新规则下无变化，分歧被显式保留。
        comparison = first["comparability"][0]
        merchant2 = next(item for item in comparison["discordant_merchants"]
                         if item["merchant_id"] == "merchant-2")
        self.assertEqual(merchant2["reference_endpoint"], "upgrade_then_fallback")
        self.assertEqual(merchant2["alternate_endpoint"], "no_change_observed")


if __name__ == "__main__":
    unittest.main()
