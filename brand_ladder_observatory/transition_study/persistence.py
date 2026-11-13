"""迁移研究在既有 SQLite registry 上的持久化。

不改动既有 runs/events/idempotency/failures 表，只新增 transition_studies、
transition_merchant_paths 两张表，并复用独立的研究级幂等键，保证同一输入
重放引用同一条分析记录（同一 study_id）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from brand_ladder_observatory.foundation.registry import stable_json, utc_now


class TransitionStudyStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS transition_studies(
                    study_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    input_fingerprint TEXT NOT NULL,
                    baseline_case_id TEXT,
                    as_of TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    reference_rule TEXT NOT NULL,
                    alternate_rule TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transition_merchant_paths(
                    study_id TEXT NOT NULL,
                    merchant_id TEXT NOT NULL,
                    cohort TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    endpoint_label TEXT NOT NULL,
                    assessable INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY(study_id, merchant_id, rule_version)
                );
                CREATE INDEX IF NOT EXISTS transition_paths_study
                    ON transition_merchant_paths(study_id);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def recall(self, request_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT result FROM transition_studies WHERE request_key=?", (request_key,)
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row["result"]) if row else None

    def find_by_study_id(self, study_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT result FROM transition_studies WHERE study_id=?", (study_id,)
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row["result"]) if row else None

    def baseline_case_exists(self, baseline_case_id: str | None) -> bool:
        if not baseline_case_id:
            return False
        connection = self._connect()
        try:
            # 迁移研究可在全新 registry 上独立运行；runs 表由既有处理器创建，
            # 尚不存在时 baseline 关联自然为 False，不能让查询中断分析。
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            if table is None:
                return False
            row = connection.execute(
                "SELECT 1 FROM runs WHERE case_id=? LIMIT 1", (baseline_case_id,)
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def save(self, study_id: str, request_key: str, input_fingerprint: str,
             baseline_case_id: str | None, as_of: str, horizon_days: int,
             reference_rule: str, alternate_rule: str, result: Mapping[str, Any],
             merchant_paths: list[Mapping[str, Any]]) -> None:
        connection = self._connect()
        try:
            existing = connection.execute(
                "SELECT study_id FROM transition_studies WHERE request_key=?", (request_key,)
            ).fetchone()
            if existing is not None:
                # 幂等重放：同一请求键必须引用同一分析记录，不重复写入。
                if existing["study_id"] != study_id:
                    raise ValueError("request_key 已关联不同 study_id，拒绝覆盖")
                return
            connection.execute(
                "INSERT INTO transition_studies(study_id,request_key,input_fingerprint,"
                "baseline_case_id,as_of,horizon_days,reference_rule,alternate_rule,result,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (study_id, request_key, input_fingerprint, baseline_case_id, as_of,
                 horizon_days, reference_rule, alternate_rule,
                 stable_json(result), utc_now()),
            )
            connection.executemany(
                "INSERT INTO transition_merchant_paths(study_id,merchant_id,cohort,sector,"
                "rule_version,endpoint_label,assessable,path) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        study_id,
                        str(item["merchant_id"]),
                        str(item["cohort"]),
                        str(item["sector"]),
                        str(item["rule_version"]),
                        str(item["endpoint_label"]),
                        1 if item["assessable"] else 0,
                        stable_json(item),
                    )
                    for item in merchant_paths
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def study_count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM transition_studies").fetchone()[0])
        finally:
            connection.close()
