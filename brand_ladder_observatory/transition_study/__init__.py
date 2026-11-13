"""面向入驻届次的品牌梯队迁移研究。

本包在不改动既有逐案处理器（静态分类、商户查询）的前提下，基于
``fixtures/analysis/input.json`` 形态的走访、退出与规则版本资料，重建同一套
分类规则下的商户状态路径，输出统一观察长度的转移与累计概率、区间估计、
届次可比性以及新旧规则的平行对照结论。
"""
from __future__ import annotations

from brand_ladder_observatory.transition_study.study import run_study

__all__ = ["run_study"]
