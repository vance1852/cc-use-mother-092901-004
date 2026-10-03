"""通勤覆盖决策工作流：版本化输入、场景冻结、方案评审与对比。

设计约束：
- 网络拓扑、班次、换乘、首末班、容量、片区六类输入只增不改，修订只能产生新版本；
- 场景创建时把引用版本整体快照冻结，方案结果在提交时计算并哈希固化，
  已发布报告不会被之后的新版本输入静默改写，变更必须通过新场景与新方案体现；
- 每个场景至多一个 approved 方案（部分唯一索引），接替旧方案会留下审计记录；
- 所有状态落 SQLite，进程重启后可继续未完成的评审与对比。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database
from .transit import DOC_TYPES, compare_plans, evaluate, normalize_params

PLAN_STATUSES = ("submitted", "approved", "rejected", "superseded")


class PlanningService:
    """在基础 :class:`~transport_coordination.service.DomainService` 之上扩展决策工作流。"""

    def __init__(self, database: Database, domain_service, clock=None) -> None:
        self.database = database
        self.domain = domain_service
        self.clock = clock or domain_service.clock

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    # -- 基础工具委托 -------------------------------------------------------

    def _actor(self, connection, actor_id: str):
        return self.domain._actor(connection, actor_id)

    def _idempotent(self, connection, **kwargs):
        return self.domain._idempotent(connection, **kwargs)

    def _load_document(self, connection, doc_type: str, doc_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM versioned_documents WHERE doc_type=? AND doc_id=? AND version=?",
            (doc_type, doc_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"输入版本不存在：{doc_type}/{doc_id}@{version}")
        return json.loads(row["payload_json"])

    @staticmethod
    def _resolve_ref(ref: Any, doc_type: str) -> tuple[str, int]:
        if isinstance(ref, dict):
            doc_id = ref.get("doc_id")
            version = ref.get("version")
        elif isinstance(ref, (list, tuple)) and len(ref) == 2:
            doc_id, version = ref
        else:
            raise ValidationError(f"{doc_type} 引用必须是 [doc_id, version]")
        doc_id = str(doc_id).strip()
        if not doc_id or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValidationError(f"{doc_type} 引用的 doc_id/version 无效")
        return doc_id, version

    # -- 版本化输入 ---------------------------------------------------------

    def register_document(self, *, request_id: str, actor_id: str, doc_type: str,
                          doc_id: str, payload: dict[str, Any], version: int | None = None):
        """登记一份只增不改的版本化输入（停运/加班/时刻修订都产生新版本）。"""

        if doc_type not in DOC_TYPES:
            raise ValidationError("doc_type 不属于六类通勤输入")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        doc_id = self.domain._identifier(doc_id, "doc_id")
        payload_hash = digest(payload)
        body = {"actor_id": actor_id, "doc_type": doc_type, "doc_id": doc_id,
                "version": version, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS latest FROM versioned_documents "
                    "WHERE doc_type=? AND doc_id=?",
                    (doc_type, doc_id),
                ).fetchone()
                next_version = row["latest"] + 1
                chosen = version if version is not None else next_version
                if chosen != next_version:
                    raise ConflictError(
                        f"{doc_type}/{doc_id} 的下一个版本必须是 {next_version}，输入只能追加不能改写")
                connection.execute(
                    "INSERT INTO versioned_documents(doc_type,doc_id,version,payload_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (doc_type, doc_id, chosen, canonical_json(payload), payload_hash,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="document.registered",
                             resource_type=f"document:{doc_type}", resource_id=doc_id,
                             detail={"doc_type": doc_type, "doc_id": doc_id, "version": chosen,
                                     "payload_hash": payload_hash},
                             occurred_at=self._now())
                return f"document:{doc_type}", f"{doc_id}@{chosen}", {
                    "doc_type": doc_type, "doc_id": doc_id, "version": chosen}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_document", payload=body, create=create)

    def list_document_versions(self, doc_type: str, doc_id: str) -> list[dict[str, Any]]:
        if doc_type not in DOC_TYPES:
            raise ValidationError("doc_type 不属于六类通勤输入")
        rows = self.database.connection.execute(
            "SELECT doc_type,doc_id,version,payload_json,payload_hash,created_by,created_at "
            "FROM versioned_documents WHERE doc_type=? AND doc_id=? ORDER BY version",
            (doc_type, doc_id),
        ).fetchall()
        if not rows:
            raise NotFoundError("输入文档不存在")
        return [{"doc_type": r["doc_type"], "doc_id": r["doc_id"], "version": r["version"],
                 "payload": json.loads(r["payload_json"]), "payload_hash": r["payload_hash"],
                 "created_by": r["created_by"], "created_at": r["created_at"]} for r in rows]

    # -- 场景冻结 -----------------------------------------------------------

    def create_scenario(self, *, request_id: str, actor_id: str, title: str,
                        refs: dict[str, Any], params: dict[str, Any] | None = None,
                        scenario_id: str | None = None):
        """把六类输入的指定版本与评估参数冻结为一个不可变场景。"""

        title = self.domain._text(title, "title")
        if not isinstance(refs, dict):
            raise ValidationError("refs 必须是对象")
        normalized_params = normalize_params(params or {})
        body = {"actor_id": actor_id, "title": title, "refs": refs,
                "params": normalized_params, "scenario_id": scenario_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")
            resolved: dict[str, list[Any]] = {}
            snapshot: dict[str, dict[str, Any]] = {}
            for doc_type in DOC_TYPES:
                if doc_type not in refs:
                    raise ValidationError(f"refs 缺少 {doc_type} 版本引用")
                ref_doc_id, ref_version = self._resolve_ref(refs[doc_type], doc_type)
                snapshot[doc_type] = self._load_document(connection, doc_type, ref_doc_id, ref_version)
                resolved[doc_type] = [ref_doc_id, ref_version]
            # 冻结前先完整跑一遍评估，拒绝内部不一致的输入组合
            evaluate(snapshot, normalized_params)
            new_id = self.domain._identifier(scenario_id or uuid.uuid4().hex, "scenario_id")
            input_hash = digest({"refs": resolved, "params": normalized_params, "snapshot": snapshot})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO scenarios(scenario_id,title,refs_json,params_json,snapshot_json,"
                        "input_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (new_id, title, canonical_json(resolved), canonical_json(normalized_params),
                         canonical_json(snapshot), input_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("场景编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="scenario.created",
                             resource_type="scenario", resource_id=new_id,
                             detail={"title": title, "refs": resolved, "input_hash": input_hash},
                             occurred_at=self._now())
                return "scenario", new_id, {"scenario_id": new_id, "input_hash": input_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_scenario", payload=body, create=create)

    def _scenario_row(self, connection, scenario_id: str):
        row = connection.execute("SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
        if row is None:
            raise NotFoundError("场景不存在")
        return row

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        row = self._scenario_row(self.database.connection, scenario_id)
        return {"scenario_id": row["scenario_id"], "title": row["title"],
                "refs": json.loads(row["refs_json"]), "params": json.loads(row["params_json"]),
                "input_hash": row["input_hash"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def list_scenarios(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT scenario_id,title,refs_json,input_hash,created_by,created_at "
            "FROM scenarios ORDER BY created_at, scenario_id").fetchall()
        return [{"scenario_id": r["scenario_id"], "title": r["title"],
                 "refs": json.loads(r["refs_json"]), "input_hash": r["input_hash"],
                 "created_by": r["created_by"], "created_at": r["created_at"]} for r in rows]

    # -- 方案提交（结果冻结） ----------------------------------------------

    def submit_plan(self, *, request_id: str, actor_id: str, scenario_id: str, name: str,
                    supersedes_plan_id: str | None = None, plan_id: str | None = None):
        """在冻结场景上计算覆盖结果并固化为待评审方案。"""

        name = self.domain._text(name, "name")
        body = {"actor_id": actor_id, "scenario_id": scenario_id, "name": name,
                "supersedes_plan_id": supersedes_plan_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")
            scenario = self._scenario_row(connection, scenario_id)
            snapshot = json.loads(scenario["snapshot_json"])
            params = json.loads(scenario["params_json"])
            if supersedes_plan_id is not None:
                previous = connection.execute(
                    "SELECT scenario_id FROM plans WHERE plan_id=?", (supersedes_plan_id,)).fetchone()
                if previous is None:
                    raise NotFoundError("被接替的方案不存在")
                if previous["scenario_id"] != scenario_id:
                    raise ValidationError("只能显式接替同一场景内的方案")
            new_id = self.domain._identifier(plan_id or uuid.uuid4().hex, "plan_id")
            result = evaluate(snapshot, params)
            result["plan_id"] = new_id
            result["scenario_id"] = scenario_id
            result_hash = digest(result)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO plans(plan_id,scenario_id,name,department_org,status,result_json,"
                        "result_hash,input_hash,supersedes_plan_id,submitted_by,submitted_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id, scenario_id, name, actor.organization_id, "submitted",
                         canonical_json(result), result_hash, scenario["input_hash"],
                         supersedes_plan_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("方案编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="plan.submitted",
                             resource_type="plan", resource_id=new_id,
                             detail={"scenario_id": scenario_id, "name": name,
                                     "department_org": actor.organization_id,
                                     "supersedes_plan_id": supersedes_plan_id,
                                     "result_hash": result_hash,
                                     "coverage_ratio": result["summary"]["coverage_ratio"]},
                             occurred_at=self._now())
                return "plan", new_id, {"plan_id": new_id, "result_hash": result_hash,
                                        "status": "submitted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_plan", payload=body, create=create)

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    @staticmethod
    def _plan_dict(row) -> dict[str, Any]:
        return {"plan_id": row["plan_id"], "scenario_id": row["scenario_id"],
                "name": row["name"], "department_org": row["department_org"],
                "status": row["status"], "result": json.loads(row["result_json"]),
                "result_hash": row["result_hash"], "input_hash": row["input_hash"],
                "supersedes_plan_id": row["supersedes_plan_id"],
                "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "decision_note": row["decision_note"]}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._plan_dict(self._plan_row(self.database.connection, plan_id))

    def list_plans(self, *, scenario_id: str | None = None, status: str | None = None,
                   department_org: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in PLAN_STATUSES:
            raise ValidationError("status 不在允许范围内")
        sql = ("SELECT plan_id,scenario_id,name,department_org,status,result_json,result_hash,"
               "input_hash,supersedes_plan_id,submitted_by,submitted_at,decided_by,decided_at,"
               "decision_note FROM plans")
        clauses, parameters = [], []
        if scenario_id:
            clauses.append("scenario_id=?")
            parameters.append(scenario_id)
        if status:
            clauses.append("status=?")
            parameters.append(status)
        if department_org:
            clauses.append("department_org=?")
            parameters.append(department_org)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY submitted_at, plan_id"
        return [self._plan_dict(r) for r in self.database.connection.execute(sql, parameters).fetchall()]

    def verify_plan(self, plan_id: str) -> dict[str, Any]:
        """用冻结场景快照重算结果，核对已发布报告未被改写。"""

        connection = self.database.connection
        row = self._plan_row(connection, plan_id)
        stored_result = json.loads(row["result_json"])
        # 先核对存储内容自身与固化哈希一致（防止只改 JSON 或只改哈希列）
        stored_intact = digest(stored_result) == row["result_hash"]
        scenario = self._scenario_row(connection, row["scenario_id"])
        snapshot = json.loads(scenario["snapshot_json"])
        params = json.loads(scenario["params_json"])
        recomputed = evaluate(snapshot, params)
        recomputed["plan_id"] = plan_id
        recomputed["scenario_id"] = row["scenario_id"]
        recomputed_hash = digest(recomputed)
        return {"plan_id": plan_id, "stored_hash": row["result_hash"],
                "stored_payload_hash": digest(stored_result),
                "recomputed_hash": recomputed_hash,
                "stored_intact": stored_intact,
                "matches": stored_intact and recomputed_hash == row["result_hash"],
                "status": row["status"]}

    # -- 评审 ---------------------------------------------------------------

    def review_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                    decision: str, note: str | None = None):
        """提交一次评审动作；approve/reject/comment/supersede。"""

        if decision not in ("approve", "reject", "comment", "supersede"):
            raise ValidationError("decision 必须是 approve/reject/comment/supersede")
        if note is not None:
            note = self.domain._text(note, "note", 1000)
        body = {"actor_id": actor_id, "plan_id": plan_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "reviewer")
            row = self._plan_row(connection, plan_id)
            if decision in ("approve", "supersede") and row["status"] == "approved":
                raise ConflictError("方案已经获批，不能重复审批")
            if decision == "reject" and row["status"] in ("rejected", "superseded"):
                raise ConflictError("已终结的方案不能再次驳回")
            if decision == "reject" and row["status"] == "approved":
                raise ConflictError("已获批方案不能驳回；如需更换请由 admin 执行 supersede 接替")
            other_approved = connection.execute(
                "SELECT plan_id FROM plans WHERE scenario_id=? AND status='approved' AND plan_id<>?",
                (row["scenario_id"], plan_id),
            ).fetchone()
            if decision == "approve" and other_approved is not None:
                raise ConflictError(
                    "该场景已有获批方案；如要接替请使用 decision=supersede（仅 admin）")
            if decision == "supersede":
                self.domain._require(actor, "admin")
                if other_approved is None:
                    raise ConflictError("当前没有已获批方案，直接 approve 即可")
            review_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO plan_reviews(review_id,plan_id,decision,note,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (review_id, plan_id, decision, note, actor_id, self._now()),
                )
                if decision == "comment":
                    new_status = row["status"]
                elif decision == "supersede":
                    new_status = "approved"
                else:
                    new_status = "approved" if decision == "approve" else "rejected"
                if decision == "supersede":
                    # 先解除旧方案的获批状态，再批准新方案，避免同场景出现两个 approved
                    prior_review = uuid.uuid4().hex
                    connection.execute(
                        "UPDATE plans SET status='superseded', decided_by=?, decided_at=?, "
                        "decision_note=? WHERE plan_id=?",
                        (actor_id, self._now(), f"被方案 {plan_id} 接替", other_approved["plan_id"]),
                    )
                    connection.execute(
                        "INSERT INTO plan_reviews(review_id,plan_id,decision,note,actor_id,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (prior_review, other_approved["plan_id"], "superseded",
                         f"被方案 {plan_id} 接替", actor_id, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="plan.superseded",
                                 resource_type="plan", resource_id=other_approved["plan_id"],
                                 detail={"by_plan_id": plan_id, "scenario_id": row["scenario_id"]},
                                 occurred_at=self._now())
                if decision in ("approve", "reject", "supersede"):
                    connection.execute(
                        "UPDATE plans SET status=?, decided_by=?, decided_at=?, decision_note=? "
                        "WHERE plan_id=?",
                        (new_status, actor_id, self._now(), note, plan_id),
                    )
                append_event(connection, actor_id=actor_id, action=f"plan.{decision}",
                             resource_type="plan", resource_id=plan_id,
                             detail={"note": note, "new_status": new_status},
                             occurred_at=self._now())
                return "plan_review", review_id, {"review_id": review_id, "plan_id": plan_id,
                                                  "decision": decision, "new_status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_plan", payload=body, create=create)

    def list_reviews(self, plan_id: str) -> list[dict[str, Any]]:
        self._plan_row(self.database.connection, plan_id)
        rows = self.database.connection.execute(
            "SELECT review_id,plan_id,decision,note,actor_id,created_at FROM plan_reviews "
            "WHERE plan_id=? ORDER BY rowid",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 方案对比 -----------------------------------------------------------

    def compare(self, *, request_id: str | None = None, actor_id: str | None = None,
                plan_a: str, plan_b: str, persist: bool = True) -> dict[str, Any]:
        """对比任意两版冻结方案；持久化比较关系以便跨部门复核。"""

        if not persist:
            row_a = self._plan_row(self.database.connection, plan_a)
            row_b = self._plan_row(self.database.connection, plan_b)
            return compare_plans(json.loads(row_a["result_json"]), json.loads(row_b["result_json"]))

        body = {"actor_id": actor_id, "plan_a": plan_a, "plan_b": plan_b}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row_a = self._plan_row(connection, plan_a)
            row_b = self._plan_row(connection, plan_b)
            pair_key = f"{plan_a}|{plan_b}"
            existing = connection.execute(
                "SELECT comparison_id,summary_json FROM plan_comparisons WHERE pair_key=?",
                (pair_key,)).fetchone()
            if existing is not None:
                comparison = json.loads(existing["summary_json"])
                comparison["comparison_id"] = existing["comparison_id"]
                return comparison
            comparison = compare_plans(json.loads(row_a["result_json"]),
                                       json.loads(row_b["result_json"]))
            comparison_id = uuid.uuid4().hex
            summary_hash = digest(comparison)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO plan_comparisons(comparison_id,plan_a,plan_b,pair_key,"
                        "summary_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (comparison_id, plan_a, plan_b, pair_key,
                         canonical_json(comparison), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("这对方案的比较关系已经存在") from exc
                append_event(connection, actor_id=actor_id, action="comparison.created",
                             resource_type="plan_comparison", resource_id=comparison_id,
                             detail={"plan_a": plan_a, "plan_b": plan_b,
                                     "summary_hash": summary_hash,
                                     "areas_gained": len(comparison["areas_gained"]),
                                     "areas_lost": len(comparison["areas_lost"])},
                             occurred_at=self._now())
                comparison["comparison_id"] = comparison_id
                return "plan_comparison", comparison_id, {"comparison_id": comparison_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="compare_plans", payload=body, create=create)
            if receipt.replayed:
                saved = connection.execute(
                    "SELECT summary_json FROM plan_comparisons WHERE pair_key=?", (pair_key,)).fetchone()
                comparison = json.loads(saved["summary_json"])
                comparison["comparison_id"] = receipt.resource_id
            else:
                comparison["comparison_id"] = receipt.resource_id
            return comparison

    def get_comparison(self, comparison_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM plan_comparisons WHERE comparison_id=?", (comparison_id,)).fetchone()
        if row is None:
            raise NotFoundError("比较记录不存在")
        payload = json.loads(row["summary_json"])
        payload["comparison_id"] = row["comparison_id"]
        payload["plan_a"] = row["plan_a"]
        payload["plan_b"] = row["plan_b"]
        payload["created_by"] = row["created_by"]
        payload["created_at"] = row["created_at"]
        return payload
