"""命令行入口：

    python3 -m brand_ladder_observatory.transition_study \
        --input <visits_json> --database <registry_db> --output <transitions_json>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from brand_ladder_observatory.foundation.registry import DomainError
from brand_ladder_observatory.transition_study.study import run_study


def main() -> int:
    parser = argparse.ArgumentParser(
        description="面向入驻届次的品牌梯队迁移研究（不规则走访、退出与规则切换）"
    )
    parser.add_argument("--input", required=True, help="走访/退出/规则输入 JSON")
    parser.add_argument("--database", required=True, help="既有 registry SQLite 数据库")
    parser.add_argument("--output", required=True, help="分析结果输出 JSON 路径")
    parser.add_argument(
        "--recency-tolerance-days",
        type=int,
        default=30,
        help="窗末仍视为在观察的末次走访宽限天数，默认 30",
    )
    args = parser.parse_args()

    try:
        result = run_study(
            input_path=args.input,
            database=args.database,
            recency_tolerance_days=args.recency_tolerance_days,
        )
    except (DomainError, ValueError, KeyError) as exc:
        parser.exit(2, f"迁移研究失败：{exc}\n")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "study_id": result["study_id"],
                "replayed": result["replayed"],
                "output": str(output_path),
                "cohorts": [item["cohort"] for item in result["cohorts"]],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
