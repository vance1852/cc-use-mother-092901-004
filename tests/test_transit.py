import unittest

from transport_coordination.errors import ValidationError
from transport_coordination.transit import compare_plans, evaluate

from commute_fixture import PARAMS, area, network_doc, snapshot


class TransitEngineTest(unittest.TestCase):
    def test_baseline_covers_both_areas_with_rides_and_segments(self):
        result = evaluate(snapshot(), PARAMS)
        self.assertEqual(1.0, result["summary"]["coverage_ratio"])
        entry = area(result, "a-a")["batches"][0]
        self.assertTrue(entry["covered"])
        self.assertEqual("07:50", entry["arrival_hhmm"])
        rides = [leg for leg in entry["path"] if leg["leg_type"] == "ride"]
        self.assertEqual(["sv-l1", "sv-l2-early"], [r["service_id"] for r in rides])
        self.assertEqual("e-l2", rides[1]["segments"][0]["edge_id"])
        transfer = next(leg for leg in entry["path"] if leg["leg_type"] == "transfer")
        self.assertEqual(8, transfer["minutes"])
        self.assertEqual(2, transfer["connect_wait_minutes"])

    def test_early_connection_miss_is_attributed_to_wait_and_transfer(self):
        result = evaluate(snapshot(early_connection=True), PARAMS)
        entry = area(result, "a-a")["batches"][0]
        self.assertFalse(entry["covered"])
        failure = entry["failure"]
        self.assertEqual("09:00", failure["deadline_hhmm"])
        self.assertEqual("08:50", failure["arrival_hhmm"])
        constraints = [c["constraint"] for c in failure["causes"]]
        self.assertIn("wait", constraints)
        self.assertIn("transfer", constraints)
        self.assertEqual("transfer", failure["primary_cause"])
        wait_at = next(c["wait_at"] for c in failure["causes"] if c["constraint"] == "wait")
        self.assertEqual("H2", wait_at["node_id"])
        self.assertTrue(wait_at["missed_connection"])
        self.assertEqual("07:25", wait_at["sched_departure_hhmm"])
        self.assertEqual("07:28", wait_at["arrive_at_platform_hhmm"])
        link = next(c["transfer_link"] for c in failure["causes"] if c["constraint"] == "transfer")
        self.assertEqual(("H1", "H2", 8), (link["from_node"], link["to_node"], link["minutes"]))
        self.assertTrue(link["connection"]["missed_connection"])

    def test_first_last_window_block_is_wait_only_cause(self):
        result = evaluate(snapshot(block_early_window=True), PARAMS)
        failure = area(result, "a-a")["batches"][0]["failure"]
        self.assertEqual(["wait"], [c["constraint"] for c in failure["causes"]])

    def test_capacity_below_threshold_blocks_boarding(self):
        blocked = evaluate(snapshot(l3_remaining=0), PARAMS)
        failure = area(blocked, "a-q")["batches"][0]["failure"]
        self.assertEqual("capacity", failure["primary_cause"])
        self.assertEqual(["capacity"], [c["constraint"] for c in failure["causes"]])
        segment = failure["causes"][0]["blocking_segment"]
        self.assertEqual(("sv-l3", "Q", "W", 0, 1),
                         (segment["service_id"], segment["from_node"], segment["to_node"],
                          segment["remaining"], segment["threshold"]))
        self.assertTrue(segment["below_threshold"])
        # 余量达到门槛即可覆盖
        ok = evaluate(snapshot(l3_remaining=1), PARAMS)
        self.assertTrue(area(ok, "a-q")["covered"])
        blocked2 = evaluate(snapshot(l3_remaining=1),
                            {**PARAMS, "capacity_threshold": 2})
        self.assertFalse(area(blocked2, "a-q")["covered"])

    def test_suspended_line_is_structurally_unreachable(self):
        result = evaluate(snapshot(suspend_l3=True), PARAMS)
        failure = area(result, "a-q")["batches"][0]["failure"]
        self.assertEqual("unreachable", failure["primary_cause"])
        self.assertTrue(failure["structural"])
        self.assertEqual(0.5, result["summary"]["coverage_ratio"])
        self.assertEqual([], failure["causes"])

    def test_area_covered_when_any_batch_works_and_bad_batch_keeps_failure(self):
        params = {**PARAMS, "batches": ["06:00", "07:00"]}
        result = evaluate(snapshot(), params)
        a_area = area(result, "a-a")
        self.assertTrue(a_area["covered"])
        self.assertIsNone(a_area["failure_summary"])
        self.assertFalse(a_area["batches"][0]["covered"])
        self.assertTrue(a_area["batches"][1]["covered"])
        self.assertEqual([0.0, 1.0],
                         [b["coverage_ratio"] for b in result["summary"]["batch_coverage"]])

    def test_compare_reports_gained_lost_and_path_changes(self):
        old = evaluate(snapshot(), PARAMS)
        old["plan_id"] = "old"
        new = evaluate(snapshot(early_connection=True, l3_remaining=0), PARAMS)
        new["plan_id"] = "new"
        diff = compare_plans(old, new)
        self.assertEqual(["a-a", "a-q"], [l["area_id"] for l in diff["areas_lost"]])
        causes = {l["area_id"]: l["primary_cause"] for l in diff["areas_lost"]}
        self.assertEqual({"a-a": "transfer", "a-q": "capacity"}, causes)
        self.assertEqual(round(0.0 - 1.0, 6), diff["coverage_ratio_delta"])
        # 反向比较就是新增
        reverse = compare_plans(new, old)
        self.assertEqual({"a-a", "a-q"}, {g["area_id"] for g in reverse["areas_gained"]})
        q_gain = next(g for g in reverse["areas_gained"] if g["area_id"] == "a-q")
        self.assertEqual(["capacity"], q_gain["relieved_constraints"])

    def test_compare_detects_changed_path_when_still_covered(self):
        # 增加 A->W 直达 50 分钟的备选线；基线走枢纽 07:50 到达
        with_alt = snapshot()
        with_alt["network"]["edges"].append(
            {"edge_id": "e-l4", "line": "L4", "from_node": "A", "to_node": "W", "travel_minutes": 50})
        with_alt["timetable"]["services"].append(
            {"service_id": "sv-l4", "line": "L4", "stops": [
                {"node_id": "A", "departure": "07:05"},
                {"node_id": "W", "arrival": "07:55"}]})
        old = evaluate(with_alt, PARAMS)
        old["plan_id"] = "old"
        # 接驳改到 07:40 发车 -> 走枢纽 08:00 到，改乘 07:55 到的直达线（仍覆盖，路径变化）
        rerouted = {**with_alt}
        rerouted["timetable"] = {**with_alt["timetable"], "services": [
            dict(s) if s["service_id"] != "sv-l2-early"
            else {"service_id": "sv-l2-late2", "line": "L2", "stops": [
                {"node_id": "H2", "departure": "07:40"},
                {"node_id": "W", "arrival": "08:00"}]}
            for s in with_alt["timetable"]["services"]
        ]}
        new = evaluate(rerouted, PARAMS)
        new["plan_id"] = "new"
        self.assertTrue(area(new, "a-a")["covered"])
        diff = compare_plans(old, new)
        changed = {c["area_id"]: c for c in diff["paths_changed"]}
        self.assertIn("a-a", changed)
        self.assertEqual(["sv-l1", "sv-l2-early"], changed["a-a"]["old_services"])
        self.assertEqual(["sv-l4"], changed["a-a"]["new_services"])
        self.assertEqual(5, changed["a-a"]["arrival_delta_minutes"])
        self.assertEqual([], diff["areas_lost"])

    def test_timetable_must_match_topology_travel_time(self):
        bad = snapshot()
        bad["timetable"]["services"][0]["stops"][1]["arrival"] = "07:25"
        with self.assertRaises(ValidationError):
            evaluate(bad, PARAMS)

    def test_zone_node_must_exist(self):
        bad = snapshot()
        bad["zone"]["residential_areas"][0]["node_ids"] = ["GHOST"]
        with self.assertRaises(ValidationError):
            evaluate(bad, PARAMS)

    def test_missing_document_is_rejected(self):
        bad = snapshot()
        del bad["capacity"]
        with self.assertRaises(ValidationError):
            evaluate(bad, PARAMS)

    def test_unknown_service_in_capacity_is_rejected(self):
        bad = snapshot()
        bad["capacity"]["segments"].append(
            {"service_id": "ghost", "from_node": "Q", "to_node": "W", "remaining": 1})
        with self.assertRaises(ValidationError):
            evaluate(bad, PARAMS)

    def test_each_ride_records_binding_remaining(self):
        doc = snapshot()
        doc["capacity"]["segments"].append(
            {"service_id": "sv-l2-early", "from_node": "H2", "to_node": "W", "remaining": 3})
        result = evaluate(doc, PARAMS)
        rides = area(result, "a-a")["batches"][0]["path"]
        l2 = next(leg for leg in rides if leg.get("service_id") == "sv-l2-early")
        self.assertEqual(3, l2["binding_remaining"])

    def test_commute_limit_breach_uses_limit_cause(self):
        # 08:50 到达，未超 09:00 截止但超过 40 分钟时限；放松单项约束也无法压到 40 分内
        result = evaluate(snapshot(early_connection=True),
                          {**PARAMS, "commute_limit": 40})
        failure = area(result, "a-a")["batches"][0]["failure"]
        self.assertEqual("commute_limit", failure["primary_cause"])
        self.assertEqual(70, failure["over_limit_minutes"])
        self.assertEqual([], failure["causes"])


if __name__ == "__main__":
    unittest.main()
