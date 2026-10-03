"""通勤覆盖决策的领域服务：场景版本、评估方案、对比评审与单一批准。

设计要点：
- 场景是不可变快照，线路停运、临时加班车、时刻修订只会派生新场景；
- 方案在创建时即完成评估并冻结结果，获批后即为发布报告，之后的运行数据
  （新场景）不会改写既有方案；
- 方案按评审事项（case）归组，多部门可并行提交，评审保留两两对比关系，
  同一事项内只允许一个方案获批（事务检查 + 部分唯一索引双重保证）；
- 评审会话持久化在 SQLite 中，进程重启后可以继续未完成的评审。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .commute import CHANGE_TYPES, compare_results, evaluate_commute, validate_snapshot
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService


class CommuteService(DomainService):
    """在基础服务之上提供通勤覆盖评估、方案冻结与审批工作流。"""

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _scenario_row(self, connection, scenario_id: str):
        row = connection.execute(
            "SELECT * FROM commute_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("场景不存在")
        return row

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT * FROM commute_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    def _review_row(self, connection, review_id: str):
        row = connection.execute(
            "SELECT * FROM commute_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("评审不存在")
        return row

    @staticmethod
    def _check_site_scope(actor: Actor, site_row) -> None:
        if actor.organization_id != site_row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    # ------------------------------------------------------------------
    # 场景：不可变的运行数据快照
    # ------------------------------------------------------------------

    def create_scenario(self, *, request_id: str, actor_id: str, site_id: str, label: str,
                        change_type: str, snapshot: dict[str, Any],
                        base_scenario_id: str | None = None,
                        change_detail: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "label": label,
                   "change_type": change_type, "snapshot": snapshot,
                   "base_scenario_id": base_scenario_id, "change_detail": change_detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            label = self._text(label, "label")
            if change_type not in CHANGE_TYPES:
                raise ValidationError("change_type 不在允许范围内")
            validate_snapshot(snapshot)
            if change_type == "initial":
                if base_scenario_id is not None:
                    raise ValidationError("初始场景不能引用基准场景")
                if change_detail is None:
                    change_detail = {}
                if not isinstance(change_detail, dict):
                    raise ValidationError("change_detail 必须是对象")
            else:
                base_scenario_id = self._identifier(str(base_scenario_id or ""), "base_scenario_id")
                base = connection.execute(
                    "SELECT scenario_id FROM commute_scenarios WHERE scenario_id=? AND site_id=?",
                    (base_scenario_id, site_id),
                ).fetchone()
                if base is None:
                    raise NotFoundError("基准场景不存在")
                if not isinstance(change_detail, dict) or not change_detail:
                    raise ValidationError("运行变化必须提供非空的 change_detail")
            snapshot_hash = digest(snapshot)

            def create() -> tuple[str, str, dict[str, Any]]:
                scenario_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO commute_scenarios(scenario_id,site_id,base_scenario_id,label,change_type,"
                    "change_detail_json,snapshot_json,snapshot_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (scenario_id, site_id, base_scenario_id, label, change_type,
                     canonical_json(change_detail), canonical_json(snapshot), snapshot_hash,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="commute_scenario.created",
                             resource_type="commute_scenario", resource_id=scenario_id,
                             detail={"site_id": site_id, "change_type": change_type,
                                     "base_scenario_id": base_scenario_id,
                                     "snapshot_hash": snapshot_hash},
                             occurred_at=self._now())
                return "commute_scenario", scenario_id, {"scenario_id": scenario_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_commute_scenario", payload=payload, create=create)

    @staticmethod
    def _scenario_meta(row) -> dict[str, Any]:
        return {"scenario_id": row["scenario_id"], "site_id": row["site_id"],
                "base_scenario_id": row["base_scenario_id"], "label": row["label"],
                "change_type": row["change_type"],
                "change_detail": json.loads(row["change_detail_json"]),
                "snapshot_hash": row["snapshot_hash"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        row = self._scenario_row(self.database.connection, scenario_id)
        item = self._scenario_meta(row)
        item["snapshot"] = json.loads(row["snapshot_json"])
        return item

    def list_scenarios(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM commute_scenarios WHERE site_id=? ORDER BY created_at, scenario_id",
            (site_id,),
        ).fetchall()
        return [self._scenario_meta(row) for row in rows]

    # ------------------------------------------------------------------
    # 方案：创建即评估并冻结，状态机 draft -> submitted -> approved/rejected
    # ------------------------------------------------------------------

    def create_plan(self, *, request_id: str, actor_id: str, site_id: str, case_id: str,
                    scenario_id: str, title: str, parameters: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "case_id": case_id,
                   "scenario_id": scenario_id, "title": title, "parameters": parameters}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_row(connection, site_id)
            case_id = self._identifier(case_id, "case_id")
            title = self._text(title, "title")
            scenario = connection.execute(
                "SELECT * FROM commute_scenarios WHERE scenario_id=? AND site_id=?",
                (scenario_id, site_id),
            ).fetchone()
            if scenario is None:
                raise NotFoundError("场景不存在")
            snapshot = json.loads(scenario["snapshot_json"])
            result = evaluate_commute(snapshot, parameters)
            result_hash = digest(result)
            coverage_ratio = result["coverage"]["ratio"]

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO commute_plans(plan_id,site_id,case_id,scenario_id,title,organization_id,"
                    "status,parameters_json,result_json,result_hash,coverage_ratio,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, site_id, case_id, scenario_id, title, actor.organization_id, "draft",
                     canonical_json(result["parameters"]), canonical_json(result), result_hash,
                     coverage_ratio, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="commute_plan.created",
                             resource_type="commute_plan", resource_id=plan_id,
                             detail={"site_id": site_id, "case_id": case_id,
                                     "scenario_id": scenario_id, "result_hash": result_hash,
                                     "coverage_ratio": coverage_ratio},
                             occurred_at=self._now())
                return "commute_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_commute_plan", payload=payload, create=create)

    def submit_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_row(connection, plan_id)
            if actor.organization_id != plan["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能提交其他部门的方案")

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "draft":
                    raise ConflictError("仅草稿状态的方案可以提交")
                connection.execute(
                    "UPDATE commute_plans SET status='submitted', submitted_at=? WHERE plan_id=?",
                    (self._now(), plan_id),
                )
                append_event(connection, actor_id=actor_id, action="commute_plan.submitted",
                             resource_type="commute_plan", resource_id=plan_id,
                             detail={"site_id": plan["site_id"], "case_id": plan["case_id"]},
                             occurred_at=self._now())
                return "commute_plan", plan_id, {"plan_id": plan_id, "status": "submitted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_commute_plan", payload=payload, create=create)

    @staticmethod
    def _plan_meta(row) -> dict[str, Any]:
        return {"plan_id": row["plan_id"], "site_id": row["site_id"], "case_id": row["case_id"],
                "scenario_id": row["scenario_id"], "title": row["title"],
                "organization_id": row["organization_id"], "status": row["status"],
                "coverage_ratio": row["coverage_ratio"], "result_hash": row["result_hash"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "submitted_at": row["submitted_at"], "decided_at": row["decided_at"],
                "decided_by": row["decided_by"]}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(self.database.connection, plan_id)
        plan = self._plan_meta(row)
        plan["parameters"] = json.loads(row["parameters_json"])
        plan["result"] = json.loads(row["result_json"])
        return plan

    def list_plans(self, site_id: str, case_id: str | None = None,
                   status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM commute_plans WHERE site_id=?"
        arguments: list[Any] = [site_id]
        if case_id:
            query += " AND case_id=?"
            arguments.append(case_id)
        if status:
            query += " AND status=?"
            arguments.append(status)
        query += " ORDER BY created_at, plan_id"
        rows = self.database.connection.execute(query, arguments).fetchall()
        return [self._plan_meta(row) for row in rows]

    # ------------------------------------------------------------------
    # 对比与评审：保留两两比较关系，裁决持久化，重启后可继续
    # ------------------------------------------------------------------

    def compare_plans(self, base_plan_id: str, candidate_plan_id: str) -> dict[str, Any]:
        base = self._plan_row(self.database.connection, base_plan_id)
        candidate = self._plan_row(self.database.connection, candidate_plan_id)
        if base["site_id"] != candidate["site_id"]:
            raise ValidationError("仅支持同一场所内的方案对比")
        comparison = compare_results(json.loads(base["result_json"]),
                                     json.loads(candidate["result_json"]))
        return {"base_plan_id": base_plan_id, "candidate_plan_id": candidate_plan_id, **comparison}

    def create_review(self, *, request_id: str, actor_id: str,
                      base_plan_id: str, candidate_plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "base_plan_id": base_plan_id,
                   "candidate_plan_id": candidate_plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            if base_plan_id == candidate_plan_id:
                raise ValidationError("评审需要两个不同的方案")
            base = self._plan_row(connection, base_plan_id)
            candidate = self._plan_row(connection, candidate_plan_id)
            if base["site_id"] != candidate["site_id"]:
                raise ValidationError("评审要求两版方案属于同一场所")
            if base["case_id"] != candidate["case_id"]:
                raise ValidationError("评审要求两版方案属于同一评审事项")

            def create() -> tuple[str, str, dict[str, Any]]:
                for plan_row in (base, candidate):
                    if plan_row["status"] not in ("submitted", "approved"):
                        raise ValidationError("仅已提交的方案可以进入评审")
                comparison = compare_results(json.loads(base["result_json"]),
                                             json.loads(candidate["result_json"]))
                comparison_hash = digest(comparison)
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO commute_reviews(review_id,site_id,case_id,base_plan_id,candidate_plan_id,"
                    "status,comparison_json,comparison_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (review_id, base["site_id"], base["case_id"], base_plan_id, candidate_plan_id,
                     "open", canonical_json(comparison), comparison_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="commute_review.created",
                             resource_type="commute_review", resource_id=review_id,
                             detail={"site_id": base["site_id"], "case_id": base["case_id"],
                                     "base_plan_id": base_plan_id, "candidate_plan_id": candidate_plan_id,
                                     "comparison_hash": comparison_hash},
                             occurred_at=self._now())
                return "commute_review", review_id, {"review_id": review_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_commute_review", payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, review_id: str,
                      decision: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "review_id": review_id,
                   "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            review = self._review_row(connection, review_id)
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 必须是 approve 或 reject")
            note = str(note or "").strip()
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")

            def create() -> tuple[str, str, dict[str, Any]]:
                if review["status"] != "open":
                    raise ConflictError("评审已完成，不能重复裁决")
                candidate = self._plan_row(connection, review["candidate_plan_id"])
                if candidate["status"] != "submitted":
                    raise ConflictError("候选方案状态已变化，无法裁决")
                now = self._now()
                if decision == "approve":
                    approved = connection.execute(
                        "SELECT COUNT(*) AS count FROM commute_plans "
                        "WHERE site_id=? AND case_id=? AND status='approved'",
                        (review["site_id"], review["case_id"]),
                    ).fetchone()["count"]
                    if approved:
                        raise ConflictError("该评审事项已存在获批方案")
                    try:
                        connection.execute(
                            "UPDATE commute_plans SET status='approved', decided_at=?, decided_by=? "
                            "WHERE plan_id=?",
                            (now, actor_id, candidate["plan_id"]),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ConflictError("该评审事项已存在获批方案") from exc
                else:
                    connection.execute(
                        "UPDATE commute_plans SET status='rejected', decided_at=?, decided_by=? "
                        "WHERE plan_id=?",
                        (now, actor_id, candidate["plan_id"]),
                    )
                connection.execute(
                    "UPDATE commute_reviews SET status='completed', decision=?, decision_note=?, "
                    "decided_by=?, decided_at=? WHERE review_id=?",
                    (decision, note, actor_id, now, review_id),
                )
                append_event(connection, actor_id=actor_id, action="commute_review.decided",
                             resource_type="commute_review", resource_id=review_id,
                             detail={"decision": decision, "note": note,
                                     "site_id": review["site_id"], "case_id": review["case_id"],
                                     "base_plan_id": review["base_plan_id"],
                                     "candidate_plan_id": review["candidate_plan_id"]},
                             occurred_at=now)
                return "commute_review", review_id, {"review_id": review_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_commute_review", payload=payload, create=create)

    @staticmethod
    def _review_meta(row, include_comparison: bool = False) -> dict[str, Any]:
        item = {"review_id": row["review_id"], "site_id": row["site_id"],
                "case_id": row["case_id"], "base_plan_id": row["base_plan_id"],
                "candidate_plan_id": row["candidate_plan_id"], "status": row["status"],
                "decision": row["decision"], "decision_note": row["decision_note"],
                "comparison_hash": row["comparison_hash"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"]}
        if include_comparison:
            item["comparison"] = json.loads(row["comparison_json"])
        return item

    def get_review(self, review_id: str) -> dict[str, Any]:
        row = self._review_row(self.database.connection, review_id)
        return self._review_meta(row, include_comparison=True)

    def list_reviews(self, site_id: str, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM commute_reviews WHERE site_id=?"
        arguments: list[Any] = [site_id]
        if status:
            query += " AND status=?"
            arguments.append(status)
        query += " ORDER BY created_at, review_id"
        rows = self.database.connection.execute(query, arguments).fetchall()
        return [self._review_meta(row) for row in rows]
