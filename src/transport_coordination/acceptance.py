"""运行基础服务与通勤覆盖决策链路的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .commute_service import CommuteService
from .storage import Database


def _snapshot(with_extra_train: bool) -> dict[str, object]:
    trips: list[dict[str, object]] = [
        {"trip_id": "T1", "line_id": "L1",
         "stops": [["A", 415, 420], ["B", 432, 433], ["C", 446, 446]]},
    ]
    if with_extra_train:
        trips.append({"trip_id": "T9", "line_id": "L1",
                      "stops": [["A", 358, 362], ["B", 374, 375], ["C", 388, 388]]})
    return {
        "stations": [
            {"station_id": "A", "name": "居住站", "zone_id": "ZA"},
            {"station_id": "B", "name": "换乘站", "zone_id": "ZB"},
            {"station_id": "C", "name": "就业站", "zone_id": "ZC"},
        ],
        "lines": [{"line_id": "L1", "name": "市域一号线", "capacity": 200}],
        "segments": [
            {"line_id": "L1", "from_station": "A", "to_station": "B", "minutes": 12},
            {"line_id": "L1", "from_station": "B", "to_station": "C", "minutes": 13},
        ],
        "trips": trips,
        "transfers": [],
        "service_windows": [{"line_id": "L1", "first_minute": 360, "last_minute": 1380}],
    }


PARAMETERS = {
    "commute_threshold_minutes": 60,
    "capacity_threshold": 0.9,
    "employment_zones": ["ZC"],
    "departure_batches": [
        {"batch_id": "B1", "origin_zone": "ZA", "depart_minute": 416, "demand": 80},
        {"batch_id": "B2", "origin_zone": "ZA", "depart_minute": 350, "demand": 60},
    ],
}


def run() -> dict[str, object]:
    """执行一条完整登记与通勤评估链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        service = CommuteService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="审批人员", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        scenario_one = service.create_scenario(request_id="req-scenario-1", actor_id="operator-001",
                                               site_id="site-001", label="现行运行图",
                                               change_type="initial", snapshot=_snapshot(False))
        plan_one = service.create_plan(request_id="req-plan-1", actor_id="operator-001",
                                       site_id="site-001", case_id="am-peak-2026q4",
                                       scenario_id=scenario_one.resource_id, title="现行方案",
                                       parameters=PARAMETERS)
        service.submit_plan(request_id="req-submit-1", actor_id="operator-001",
                            plan_id=plan_one.resource_id)
        scenario_two = service.create_scenario(request_id="req-scenario-2", actor_id="operator-001",
                                               site_id="site-001", label="加班车运行图",
                                               change_type="extra_train",
                                               base_scenario_id=scenario_one.resource_id,
                                               change_detail={"trip_id": "T9"},
                                               snapshot=_snapshot(True))
        plan_two = service.create_plan(request_id="req-plan-2", actor_id="operator-001",
                                       site_id="site-001", case_id="am-peak-2026q4",
                                       scenario_id=scenario_two.resource_id, title="加班车方案",
                                       parameters=PARAMETERS)
        service.submit_plan(request_id="req-submit-2", actor_id="operator-001",
                            plan_id=plan_two.resource_id)
        review = service.create_review(request_id="req-review-1", actor_id="reviewer-001",
                                       base_plan_id=plan_one.resource_id,
                                       candidate_plan_id=plan_two.resource_id)

        # 模拟进程重启：关闭数据库后重开，继续未完成的评审。
        database.close()
        database = Database(path)
        service = CommuteService(database, clock)
        open_reviews = service.list_reviews("site-001", status="open")
        service.decide_review(request_id="req-decision-1", actor_id="reviewer-001",
                              review_id=open_reviews[0]["review_id"],
                              decision="approve", note="同意加班车方案")
        approved = service.get_plan(plan_two.resource_id)
        comparison = service.get_review(review.resource_id)["comparison"]

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "commute": {"plans": len(service.list_plans("site-001")),
                              "open_reviews_after_restart": len(open_reviews),
                              "approved_plan_id": approved["plan_id"],
                              "approved_coverage": approved["result"]["coverage"]["ratio"],
                              "gained": comparison["summary"]["counts"]["gained"]}}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    commute = result["commute"]
    ok = (result["status"] == "ok" and result["audit_valid"]
          and commute["open_reviews_after_restart"] == 1 and commute["gained"] == 1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
