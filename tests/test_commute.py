import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.api import route
from transport_coordination.audit import digest
from transport_coordination.clock import FixedClock
from transport_coordination.commute import compare_results, evaluate_commute
from transport_coordination.commute_service import CommuteService
from transport_coordination.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.storage import Database

CLOCK = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))


def base_snapshot(**overrides):
    snapshot = {
        "stations": [
            {"station_id": "R1", "name": "居住一", "zone_id": "ZR"},
            {"station_id": "M1", "name": "枢纽", "zone_id": "ZM"},
            {"station_id": "J1", "name": "就业一", "zone_id": "ZJ"},
            {"station_id": "F1", "name": "外围", "zone_id": "ZF"},
        ],
        "lines": [
            {"line_id": "L1", "name": "市域1号", "capacity": 200},
            {"line_id": "L2", "name": "城际2号", "capacity": 100},
        ],
        "segments": [
            {"line_id": "L1", "from_station": "R1", "to_station": "M1", "minutes": 10},
            {"line_id": "L1", "from_station": "M1", "to_station": "J1", "minutes": 10},
            {"line_id": "L2", "from_station": "M1", "to_station": "F1", "minutes": 15},
        ],
        "trips": [
            {"trip_id": "T1", "line_id": "L1",
             "stops": [["R1", 415, 420], ["M1", 430, 431], ["J1", 441, 441]]},
            {"trip_id": "T2", "line_id": "L1",
             "stops": [["R1", 445, 450], ["M1", 460, 461], ["J1", 471, 471]]},
            {"trip_id": "T3", "line_id": "L2",
             "stops": [["M1", 435, 436], ["F1", 451, 451]]},
        ],
        "transfers": [
            {"station_id": "M1", "from_line": "L1", "to_line": "L2", "walk_minutes": 4},
        ],
        "service_windows": [{"line_id": "L1", "first_minute": 360, "last_minute": 1320}],
    }
    snapshot.update(overrides)
    return snapshot


def base_parameters(**overrides):
    parameters = {
        "commute_threshold_minutes": 60,
        "capacity_threshold": 0.8,
        "max_transfer_minutes": 30,
        "employment_zones": ["ZJ", "ZF"],
        "departure_batches": [
            {"batch_id": "B1", "origin_zone": "ZR", "depart_minute": 416, "demand": 50},
        ],
    }
    parameters.update(overrides)
    return parameters


def outcome_map(result):
    return {(item["batch_id"], item["zone_id"]): item for item in result["outcomes"]}


class EngineTest(unittest.TestCase):
    def test_covered_pairs_freeze_path_wait_and_margin(self):
        result = evaluate_commute(base_snapshot(), base_parameters())
        outcomes = outcome_map(result)
        direct = outcomes[("B1", "ZJ")]
        self.assertEqual("covered", direct["status"])
        self.assertEqual(441, direct["arrive_minute"])
        self.assertEqual(25, direct["duration_minutes"])
        self.assertEqual(4, direct["total_wait_minutes"])
        self.assertEqual(0.75, direct["crowding_margin"])
        self.assertEqual("T1", direct["path"]["legs"][0]["trip_id"])
        transfer = outcomes[("B1", "ZF")]
        self.assertEqual("covered", transfer["status"])
        self.assertEqual(35, transfer["duration_minutes"])
        self.assertEqual(6, transfer["total_wait_minutes"])
        self.assertEqual(0.5, transfer["crowding_margin"])
        self.assertEqual([{"station": "M1", "from_line": "L1", "to_line": "L2", "walk_minutes": 4}],
                         transfer["path"]["transfers"])
        self.assertEqual(1.0, result["coverage"]["ratio"])
        self.assertEqual(1.0, result["coverage"]["demand_ratio"])

    def test_waiting_attribution_when_departure_before_first_train(self):
        result = evaluate_commute(base_snapshot(), base_parameters(
            employment_zones=["ZJ"],
            departure_batches=[{"batch_id": "B0", "origin_zone": "ZR",
                                "depart_minute": 300, "demand": 20}],
        ))
        outcome = outcome_map(result)[("B0", "ZJ")]
        self.assertEqual("uncovered", outcome["status"])
        reason = outcome["reason"]
        self.assertEqual("waiting", reason["type"])
        self.assertEqual("arrival_beyond_threshold", reason["issue"])
        self.assertEqual(441, reason["earliest_arrive_minute"])
        self.assertEqual(81, reason["overshoot_minutes"])
        self.assertEqual({"station": "R1", "line_id": "L1", "wait_minutes": 120},
                         reason["longest_wait"])

    def test_capacity_attribution_when_all_trips_full(self):
        result = evaluate_commute(base_snapshot(), base_parameters(
            employment_zones=["ZJ"],
            departure_batches=[{"batch_id": "B1", "origin_zone": "ZR",
                                "depart_minute": 416, "demand": 170}],
        ))
        outcome = outcome_map(result)[("B1", "ZJ")]
        self.assertEqual("uncovered", outcome["status"])
        reason = outcome["reason"]
        self.assertEqual("capacity", reason["type"])
        self.assertEqual("T1", reason["trip_id"])
        self.assertEqual(160, reason["remaining"])
        self.assertEqual(170, reason["required"])

    def test_capacity_overflow_falls_back_to_next_trip(self):
        result = evaluate_commute(base_snapshot(), base_parameters(
            employment_zones=["ZJ"],
            departure_batches=[
                {"batch_id": "B1", "origin_zone": "ZR", "depart_minute": 416, "demand": 100},
                {"batch_id": "B2", "origin_zone": "ZR", "depart_minute": 416, "demand": 100},
            ],
        ))
        outcomes = outcome_map(result)
        self.assertEqual("T1", outcomes[("B1", "ZJ")]["path"]["legs"][0]["trip_id"])
        second = outcomes[("B2", "ZJ")]
        self.assertEqual("covered", second["status"])
        self.assertEqual("T2", second["path"]["legs"][0]["trip_id"])
        self.assertEqual(471, second["arrive_minute"])

    def test_transfer_attribution_when_rule_missing(self):
        result = evaluate_commute(base_snapshot(transfers=[]), base_parameters())
        outcomes = outcome_map(result)
        self.assertEqual("covered", outcomes[("B1", "ZJ")]["status"])
        reason = outcomes[("B1", "ZF")]["reason"]
        self.assertEqual("transfer", reason["type"])
        self.assertEqual("no_transfer_rule", reason["issue"])
        self.assertEqual("M1", reason["station"])
        self.assertEqual("L1", reason["from_line"])
        self.assertEqual("L2", reason["to_line"])
        self.assertEqual(0.5, result["coverage"]["ratio"])

    def test_transfer_attribution_when_walk_exceeds_limit(self):
        result = evaluate_commute(base_snapshot(), base_parameters(max_transfer_minutes=3))
        reason = outcome_map(result)[("B1", "ZF")]["reason"]
        self.assertEqual("transfer", reason["type"])
        self.assertEqual("walk_exceeds_limit", reason["issue"])
        self.assertEqual(4, reason["walk_minutes"])

    def test_service_window_blocks_boarding(self):
        snapshot = base_snapshot(
            service_windows=[{"line_id": "L1", "first_minute": 480, "last_minute": 1320}])
        result = evaluate_commute(snapshot, base_parameters(employment_zones=["ZJ"]))
        reason = outcome_map(result)[("B1", "ZJ")]["reason"]
        self.assertEqual("waiting", reason["type"])
        self.assertEqual("no_reachable_trip", reason["issue"])

    def test_compare_reports_gained_and_lost_with_causes(self):
        old_result = evaluate_commute(base_snapshot(), base_parameters(
            employment_zones=["ZJ"],
            departure_batches=[{"batch_id": "B2", "origin_zone": "ZR",
                                "depart_minute": 350, "demand": 60}],
        ))
        extra_train = {"trip_id": "T0", "line_id": "L1",
                       "stops": [["R1", 358, 362], ["M1", 372, 373], ["J1", 383, 383]]}
        new_snapshot = base_snapshot(trips=[extra_train] + base_snapshot()["trips"])
        new_result = evaluate_commute(new_snapshot, base_parameters(
            employment_zones=["ZJ"],
            departure_batches=[{"batch_id": "B2", "origin_zone": "ZR",
                                "depart_minute": 350, "demand": 60}],
        ))
        comparison = compare_results(old_result, new_result)
        self.assertEqual(1, comparison["summary"]["counts"]["gained"])
        change = comparison["changes"][0]
        self.assertEqual("gained", change["change"])
        self.assertEqual("waiting", change["cause"]["type"])
        self.assertEqual(1.0, comparison["summary"]["coverage_delta"])

        suspended = base_snapshot(trips=[base_snapshot()["trips"][2]])
        suspended_result = evaluate_commute(suspended, base_parameters())
        comparison = compare_results(evaluate_commute(base_snapshot(), base_parameters()),
                                     suspended_result)
        self.assertEqual(2, comparison["summary"]["counts"]["lost"])
        for change in comparison["changes"]:
            self.assertEqual("lost", change["change"])
            self.assertEqual("waiting", change["cause"]["type"])

        same = compare_results(old_result, old_result)
        self.assertEqual(1, same["summary"]["counts"]["kept_uncovered"])
        self.assertEqual(0, same["summary"]["coverage_delta"])

    def test_snapshot_validation_rejects_bad_references(self):
        with self.assertRaises(ValidationError):
            evaluate_commute(base_snapshot(trips=[{"trip_id": "TX", "line_id": "L1",
                                                   "stops": [["R1", 1, 2], ["ZZ", 3, 3]]}]),
                             base_parameters())
        with self.assertRaises(ValidationError):
            evaluate_commute(base_snapshot(), base_parameters(employment_zones=["ZZ"]))
        with self.assertRaises(ValidationError):
            evaluate_commute(base_snapshot(), base_parameters(
                departure_batches=[{"batch_id": "B1", "origin_zone": "ZR",
                                    "depart_minute": 416, "demand": 0}]))


def boot(service):
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="运营机构一")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                           display_name="操作员", role="operator", organization_id="o1")
    service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                           display_name="审批员", role="reviewer", organization_id="o1")
    service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                           display_name="审计员", role="auditor", organization_id="o1")
    service.register_site(request_id="site", actor_id="op1", site_id="s1",
                          organization_id="o1", name="交通节点", timezone_name="Asia/Shanghai")


class CommuteServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CommuteService(self.database, CLOCK)
        boot(self.service)
        self.scenario_v1 = self.service.create_scenario(
            request_id="scn-1", actor_id="op1", site_id="s1", label="现运行图",
            change_type="initial", snapshot=base_snapshot()).resource_id

    def tearDown(self):
        self.database.close()

    def _plan(self, request_id, scenario_id=None, actor="op1", case="case-1",
              title="方案", parameters=None):
        return self.service.create_plan(
            request_id=request_id, actor_id=actor, site_id="s1", case_id=case,
            scenario_id=scenario_id or self.scenario_v1, title=title,
            parameters=parameters or base_parameters()).resource_id

    def _submit(self, request_id, plan_id, actor="op1"):
        self.service.submit_plan(request_id=request_id, actor_id=actor, plan_id=plan_id)

    def test_plan_freezes_paths_margins_and_reasons(self):
        plan_id = self._plan("plan-1")
        plan = self.service.get_plan(plan_id)
        self.assertEqual("draft", plan["status"])
        self.assertEqual(1.0, plan["result"]["coverage"]["ratio"])
        self.assertEqual(digest(plan["result"]), plan["result_hash"])
        outcomes = outcome_map(plan["result"])
        self.assertEqual(0.75, outcomes[("B1", "ZJ")]["crowding_margin"])
        self.assertEqual(4, outcomes[("B1", "ZF")]["path"]["transfers"][0]["walk_minutes"])

    def test_operational_changes_never_rewrite_published_plan(self):
        plan_one = self._plan("p1")
        self._submit("s1", plan_one)
        scenario_v2 = self.service.create_scenario(
            request_id="scn-2", actor_id="op1", site_id="s1", label="加班车",
            change_type="extra_train", base_scenario_id=self.scenario_v1,
            change_detail={"trip_id": "T0"},
            snapshot=base_snapshot(trips=[{"trip_id": "T0", "line_id": "L1",
                                           "stops": [["R1", 400, 405], ["M1", 415, 416],
                                                     ["J1", 426, 426]]}] + base_snapshot()["trips"]),
        ).resource_id
        plan_two = self._plan("p2", scenario_id=scenario_v2)
        self._submit("s2", plan_two)
        review = self.service.create_review(request_id="r1", actor_id="rv1",
                                            base_plan_id=plan_one,
                                            candidate_plan_id=plan_two).resource_id
        self.service.decide_review(request_id="d1", actor_id="rv1", review_id=review,
                                   decision="approve", note="同意")
        published_hash = self.service.get_plan(plan_two)["result_hash"]

        self.service.create_scenario(
            request_id="scn-3", actor_id="op1", site_id="s1", label="线路停运",
            change_type="line_suspension", base_scenario_id=scenario_v2,
            change_detail={"line_id": "L1"},
            snapshot=base_snapshot(trips=[base_snapshot()["trips"][2]]))

        published = self.service.get_plan(plan_two)
        self.assertEqual("approved", published["status"])
        self.assertEqual(published_hash, published["result_hash"])
        self.assertEqual(1.0, published["result"]["coverage"]["ratio"])
        self.assertEqual(base_snapshot(), self.service.get_scenario(self.scenario_v1)["snapshot"])
        self.assertEqual(3, len(self.service.list_scenarios("s1")))
        valid, _count = self.service.verify_audit()
        self.assertTrue(valid)

    def test_only_one_plan_approved_per_case(self):
        plan_one = self._plan("p1")
        plan_two = self._plan("p2")
        self._submit("s1", plan_one)
        self._submit("s2", plan_two)
        review_one = self.service.create_review(request_id="r1", actor_id="rv1",
                                                base_plan_id=plan_one,
                                                candidate_plan_id=plan_two).resource_id
        self.service.decide_review(request_id="d1", actor_id="rv1",
                                   review_id=review_one, decision="approve")
        self.assertEqual("approved", self.service.get_plan(plan_two)["status"])

        plan_three = self._plan("p3")
        self._submit("s3", plan_three)
        review_two = self.service.create_review(request_id="r2", actor_id="rv1",
                                                base_plan_id=plan_two,
                                                candidate_plan_id=plan_three).resource_id
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="d2", actor_id="rv1",
                                       review_id=review_two, decision="approve")
        self.service.decide_review(request_id="d3", actor_id="rv1",
                                   review_id=review_two, decision="reject", note="退回")
        self.assertEqual("rejected", self.service.get_plan(plan_three)["status"])
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="d4", actor_id="rv1",
                                       review_id=review_two, decision="approve")

    def test_multiple_departments_submit_and_compare(self):
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="规划院")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="规划师", role="operator", organization_id="o2")
        plan_one = self._plan("p1", actor="op1")
        plan_two = self._plan("p2", actor="op2")
        with self.assertRaises(PermissionDenied):
            self._submit("sx", plan_two, actor="op1")
        self._submit("s1", plan_one, actor="op1")
        self._submit("s2", plan_two, actor="op2")
        review = self.service.create_review(request_id="r1", actor_id="rv1",
                                            base_plan_id=plan_one,
                                            candidate_plan_id=plan_two).resource_id
        comparison = self.service.get_review(review)["comparison"]
        self.assertEqual(2, comparison["summary"]["counts"]["kept_covered"])
        plans = self.service.list_plans("s1", case_id="case-1")
        self.assertEqual({"o1", "o2"}, {plan["organization_id"] for plan in plans})

    def test_review_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commute.sqlite3"
            service = CommuteService(Database(path), CLOCK)
            boot(service)
            scenario = service.create_scenario(
                request_id="scn", actor_id="op1", site_id="s1", label="现图",
                change_type="initial", snapshot=base_snapshot()).resource_id
            plan_one = service.create_plan(request_id="p1", actor_id="op1", site_id="s1",
                                           case_id="case-1", scenario_id=scenario, title="一",
                                           parameters=base_parameters()).resource_id
            plan_two = service.create_plan(request_id="p2", actor_id="op1", site_id="s1",
                                           case_id="case-1", scenario_id=scenario, title="二",
                                           parameters=base_parameters()).resource_id
            service.submit_plan(request_id="s1", actor_id="op1", plan_id=plan_one)
            service.submit_plan(request_id="s2", actor_id="op1", plan_id=plan_two)
            review = service.create_review(request_id="r1", actor_id="rv1",
                                           base_plan_id=plan_one,
                                           candidate_plan_id=plan_two).resource_id
            service.database.close()

            resumed = CommuteService(Database(path), CLOCK)
            open_reviews = resumed.list_reviews("s1", status="open")
            self.assertEqual([review], [item["review_id"] for item in open_reviews])
            self.assertIn("comparison", resumed.get_review(review))
            resumed.decide_review(request_id="d1", actor_id="rv1", review_id=review,
                                  decision="approve", note="重启后批准")
            self.assertEqual("completed", resumed.get_review(review)["status"])
            self.assertEqual("approved", resumed.get_plan(plan_two)["status"])
            valid, _count = resumed.verify_audit()
            self.assertTrue(valid)
            resumed.database.close()

    def test_concurrent_approval_keeps_single_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            seed = CommuteService(Database(path), CLOCK)
            boot(seed)
            scenario = seed.create_scenario(
                request_id="scn", actor_id="op1", site_id="s1", label="现图",
                change_type="initial", snapshot=base_snapshot()).resource_id
            plan_one = seed.create_plan(request_id="p1", actor_id="op1", site_id="s1",
                                        case_id="race", scenario_id=scenario, title="一",
                                        parameters=base_parameters()).resource_id
            plan_two = seed.create_plan(request_id="p2", actor_id="op1", site_id="s1",
                                        case_id="race", scenario_id=scenario, title="二",
                                        parameters=base_parameters()).resource_id
            seed.submit_plan(request_id="s1", actor_id="op1", plan_id=plan_one)
            seed.submit_plan(request_id="s2", actor_id="op1", plan_id=plan_two)
            review_one = seed.create_review(request_id="r1", actor_id="rv1",
                                            base_plan_id=plan_one,
                                            candidate_plan_id=plan_two).resource_id
            review_two = seed.create_review(request_id="r2", actor_id="rv1",
                                            base_plan_id=plan_two,
                                            candidate_plan_id=plan_one).resource_id
            seed.database.close()

            results = []
            barrier = threading.Barrier(2)

            def decide(review_id, request_id):
                service = CommuteService(Database(path), CLOCK)
                try:
                    barrier.wait(timeout=10)
                    service.decide_review(request_id=request_id, actor_id="rv1",
                                          review_id=review_id, decision="approve")
                    results.append("ok")
                except ConflictError:
                    results.append("conflict")
                finally:
                    service.database.close()

            threads = [threading.Thread(target=decide, args=(review_one, "td-1")),
                       threading.Thread(target=decide, args=(review_two, "td-2"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(["conflict", "ok"], sorted(results))
            check = CommuteService(Database(path), CLOCK)
            approved = [plan for plan in check.list_plans("s1", case_id="race")
                        if plan["status"] == "approved"]
            self.assertEqual(1, len(approved))
            check.database.close()

    def test_writes_are_idempotent(self):
        first = self.service.create_scenario(
            request_id="dup-scn", actor_id="op1", site_id="s1", label="重复",
            change_type="initial", snapshot=base_snapshot())
        second = self.service.create_scenario(
            request_id="dup-scn", actor_id="op1", site_id="s1", label="重复",
            change_type="initial", snapshot=base_snapshot())
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

        plan_id = self._plan("dup-plan")
        replay = self.service.create_plan(
            request_id="dup-plan", actor_id="op1", site_id="s1", case_id="case-1",
            scenario_id=self.scenario_v1, title="方案", parameters=base_parameters())
        self.assertTrue(replay.replayed)
        self.assertEqual(plan_id, replay.resource_id)

        self._submit("sub", plan_id)
        other = self._plan("other")
        self._submit("sub-2", other)
        review = self.service.create_review(request_id="rev", actor_id="rv1",
                                            base_plan_id=other, candidate_plan_id=plan_id).resource_id
        decision = self.service.decide_review(request_id="dec", actor_id="rv1",
                                              review_id=review, decision="approve")
        replayed = self.service.decide_review(request_id="dec", actor_id="rv1",
                                              review_id=review, decision="approve")
        self.assertFalse(decision.replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual("approved", self.service.get_plan(plan_id)["status"])

    def test_validation_and_permission_rules(self):
        with self.assertRaises(ValidationError):
            self.service.create_scenario(request_id="bad-1", actor_id="op1", site_id="s1",
                                         label="坏", change_type="unknown",
                                         snapshot=base_snapshot())
        with self.assertRaises(ValidationError):
            self.service.create_scenario(request_id="bad-2", actor_id="op1", site_id="s1",
                                         label="坏", change_type="timetable_revision",
                                         snapshot=base_snapshot(),
                                         change_detail={"trip_id": "T1"})
        with self.assertRaises(NotFoundError):
            self.service.create_scenario(request_id="bad-3", actor_id="op1", site_id="s1",
                                         label="坏", change_type="timetable_revision",
                                         base_scenario_id="missing",
                                         snapshot=base_snapshot(),
                                         change_detail={"trip_id": "T1"})
        with self.assertRaises(PermissionDenied):
            self.service.create_scenario(request_id="bad-4", actor_id="au1", site_id="s1",
                                         label="坏", change_type="initial",
                                         snapshot=base_snapshot())
        with self.assertRaises(PermissionDenied):
            self._plan("bad-5", actor="rv1")
        draft = self._plan("draft")
        submitted = self._plan("submitted")
        self._submit("sub-9", submitted)
        with self.assertRaises(ValidationError):
            self.service.create_review(request_id="bad-6", actor_id="rv1",
                                       base_plan_id=draft, candidate_plan_id=submitted)
        with self.assertRaises(PermissionDenied):
            self.service.create_review(request_id="bad-7", actor_id="op1",
                                       base_plan_id=submitted, candidate_plan_id=submitted)


class CommuteApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CommuteService(self.database, CLOCK)
        boot(self.service)

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def _get(self, path):
        return route(self.service, "GET", path, None)

    def test_full_review_flow_over_http(self):
        status, scenario = self._post("/commute/scenarios", {
            "request_id": "api-scn", "site_id": "s1", "label": "现图",
            "change_type": "initial", "snapshot": base_snapshot()})
        self.assertEqual(201, status)
        scenario_id = scenario["resource_id"]
        status, replay = self._post("/commute/scenarios", {
            "request_id": "api-scn", "site_id": "s1", "label": "现图",
            "change_type": "initial", "snapshot": base_snapshot()})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        status, fetched = self._get(f"/commute/scenarios/{scenario_id}")
        self.assertEqual(200, status)
        self.assertEqual("initial", fetched["change_type"])
        self.assertIn("snapshot", fetched)

        plan_ids = []
        for index in (1, 2):
            status, plan = self._post("/commute/plans", {
                "request_id": f"api-plan-{index}", "site_id": "s1", "case_id": "case-1",
                "scenario_id": scenario_id, "title": f"方案{index}",
                "parameters": base_parameters()})
            self.assertEqual(201, status)
            plan_ids.append(plan["resource_id"])
            status, _ = self._post(f"/commute/plans/{plan['resource_id']}/submit",
                                   {"request_id": f"api-sub-{index}"})
            self.assertEqual(201, status)

        status, plan = self._get(f"/commute/plans/{plan_ids[0]}")
        self.assertEqual(200, status)
        self.assertEqual("submitted", plan["status"])
        self.assertEqual(1.0, plan["result"]["coverage"]["ratio"])

        status, comparison = self._get(
            f"/commute/compare?base_plan_id={plan_ids[0]}&candidate_plan_id={plan_ids[1]}")
        self.assertEqual(200, status)
        self.assertEqual(2, comparison["summary"]["counts"]["kept_covered"])

        status, review = self._post("/commute/reviews", {
            "request_id": "api-review", "base_plan_id": plan_ids[0],
            "candidate_plan_id": plan_ids[1]}, actor="rv1")
        self.assertEqual(201, status)
        review_id = review["resource_id"]

        status, listing = self._get("/commute/reviews?site_id=s1&status=open")
        self.assertEqual(200, status)
        self.assertEqual([review_id], [item["review_id"] for item in listing["items"]])

        status, _ = self._post(f"/commute/reviews/{review_id}/decision",
                               {"request_id": "api-decision", "decision": "approve",
                                "note": "同意"}, actor="rv1")
        self.assertEqual(201, status)
        status, plan = self._get(f"/commute/plans/{plan_ids[1]}")
        self.assertEqual("approved", plan["status"])
        status, review = self._get(f"/commute/reviews/{review_id}")
        self.assertEqual("completed", review["status"])
        self.assertIn("comparison", review)

    def test_commute_route_errors(self):
        status, payload = self._get("/commute/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])
        status, payload = self._post("/commute/scenarios", {
            "request_id": "denied", "site_id": "s1", "label": "x",
            "change_type": "initial", "snapshot": base_snapshot()}, actor="au1")
        self.assertEqual(403, status)
        status, payload = self._post("/commute/scenarios", {
            "request_id": "invalid", "site_id": "s1", "label": "x",
            "change_type": "initial", "snapshot": {"stations": []}})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])
        status, payload = self._get("/commute/plans/missing")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
