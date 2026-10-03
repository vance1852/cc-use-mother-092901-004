"""运行基础服务与通勤覆盖决策的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .planning import PlanningService
from .service import DomainService
from .storage import Database


def _network():
    return {
        "nodes": [
            {"node_id": "A", "name": "外围居住站"},
            {"node_id": "H1", "name": "枢纽市域台"},
            {"node_id": "H2", "name": "枢纽城际台"},
            {"node_id": "W", "name": "中心就业站"},
            {"node_id": "Q", "name": "另一居住站"},
        ],
        "edges": [
            {"edge_id": "e-l1", "line": "L1", "from_node": "A", "to_node": "H1", "travel_minutes": 20},
            {"edge_id": "e-l2", "line": "L2", "from_node": "H2", "to_node": "W", "travel_minutes": 20},
            {"edge_id": "e-l3", "line": "L3", "from_node": "Q", "to_node": "W", "travel_minutes": 30},
        ],
    }


def _timetable(*, early: bool):
    dep, arr = ("07:25", "07:45") if early else ("07:30", "07:50")
    return {"services": [
        {"service_id": "sv-l1", "line": "L1", "stops": [
            {"node_id": "A", "departure": "07:00"}, {"node_id": "H1", "arrival": "07:20"}]},
        {"service_id": "sv-l2-conn", "line": "L2", "stops": [
            {"node_id": "H2", "departure": dep}, {"node_id": "W", "arrival": arr}]},
        {"service_id": "sv-l2-late", "line": "L2", "stops": [
            {"node_id": "H2", "departure": "08:30"}, {"node_id": "W", "arrival": "08:50"}]},
        {"service_id": "sv-l3", "line": "L3", "stops": [
            {"node_id": "Q", "departure": "07:10"}, {"node_id": "W", "arrival": "07:40"}]},
    ]}


def _zones():
    return {
        "employment_zones": [{"zone_id": "z-w", "name": "中心就业片区", "node_ids": ["W"]}],
        "residential_areas": [
            {"area_id": "a-a", "name": "A 居住片", "node_ids": ["A"]},
            {"area_id": "a-q", "name": "Q 居住片", "node_ids": ["Q"]},
        ],
    }


PARAMS = {"deadline": "09:00", "commute_limit": 60, "capacity_threshold": 1,
          "batches": ["07:00"]}


def run() -> dict[str, object]:
    """执行登记链与完整通勤决策链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)))
        planning = PlanningService(database, service, service.clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="审批人", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 通勤决策链：六类输入 v1 -> 旧场景/旧方案 ----
        docs_v1 = {"network": _network(), "timetable": _timetable(early=False),
                   "transfer": {"transfers": [{"from_node": "H1", "to_node": "H2", "minutes": 8}]},
                   "service_window": {"windows": []},
                   "capacity": {"segments": [{"service_id": "sv-l3", "from_node": "Q",
                                              "to_node": "W", "remaining": 100}]},
                   "zone": _zones()}
        refs_old = {}
        for doc_type, payload in docs_v1.items():
            receipt = planning.register_document(
                request_id=f"req-doc-old-{doc_type}", actor_id="operator-001",
                doc_type=doc_type, doc_id=f"{doc_type}-main", payload=payload)
            refs_old[doc_type] = [f"{doc_type}-main", 1]
            assert not receipt.replayed
        planning.create_scenario(request_id="req-scn-old", actor_id="operator-001",
                                 title="现行宣传时刻", refs=refs_old, params=PARAMS,
                                 scenario_id="scn-old")
        planning.submit_plan(request_id="req-plan-old", actor_id="operator-001",
                             scenario_id="scn-old", name="现行宣传方案", plan_id="plan-old")
        planning.review_plan(request_id="req-ap-old", actor_id="reviewer-001",
                             plan_id="plan-old", decision="approve", note="与现行宣传一致")

        # ---- 时刻修订只能追加新版本，冻结新场景 ----
        planning.register_document(
            request_id="req-doc-new-tt", actor_id="operator-001", doc_type="timetable",
            doc_id="timetable-main", payload=_timetable(early=True))
        refs_new = dict(refs_old)
        refs_new["timetable"] = ["timetable-main", 2]
        planning.create_scenario(request_id="req-scn-new", actor_id="operator-001",
                                 title="城际提前发车修订", refs=refs_new, params=PARAMS,
                                 scenario_id="scn-new")
        planning.submit_plan(request_id="req-plan-new", actor_id="operator-001",
                             scenario_id="scn-new", name="修订后方案", plan_id="plan-new")

        old_plan = planning.get_plan("plan-old")
        new_plan = planning.get_plan("plan-new")
        old_ratio = old_plan["result"]["summary"]["coverage_ratio"]
        new_ratio = new_plan["result"]["summary"]["coverage_ratio"]
        # 旧报告未被新时刻改写；修订后 A 片区因换乘错位失去早高峰覆盖
        assert old_ratio == 1.0
        assert new_ratio == 0.5
        assert planning.verify_plan("plan-old")["matches"]
        assert planning.verify_plan("plan-new")["matches"]

        comparison = planning.compare(request_id="req-cmp", actor_id="reviewer-001",
                                      plan_a="plan-old", plan_b="plan-new")
        assert [l["area_id"] for l in comparison["areas_lost"]] == ["a-a"]
        lost = comparison["areas_lost"][0]
        assert lost["primary_cause"] == "transfer"
        assert "wait" in lost["causes"]

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "old_coverage_ratio": old_ratio,
                  "new_coverage_ratio": new_ratio,
                  "lost_areas": [l["area_id"] for l in comparison["areas_lost"]],
                  "lost_primary_cause": lost["primary_cause"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
