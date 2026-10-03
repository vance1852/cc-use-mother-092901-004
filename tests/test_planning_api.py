import unittest

from transport_coordination.api import route
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

from commute_fixture import PARAMS, snapshot


class PlanningApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.requests = {}

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body, {"X-Actor-Id": actor})

    def _req(self, key):
        value = f"req-{key}"
        return value

    def bootstrap(self):
        self.call("POST", "/organizations",
                  {"request_id": "r-org", "organization_id": "o1", "name": "规划机构"}, "bootstrap")
        self.call("POST", "/actors",
                  {"request_id": "r-admin", "new_actor_id": "a1", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"}, "bootstrap")
        self.call("POST", "/actors",
                  {"request_id": "r-op", "new_actor_id": "op1", "display_name": "规划师",
                   "role": "operator", "organization_id": "o1"}, "a1")
        self.call("POST", "/actors",
                  {"request_id": "r-rev", "new_actor_id": "rev1", "display_name": "审批人",
                   "role": "reviewer", "organization_id": "o1"}, "a1")

    def seed_scenario(self, req_prefix="s1", scenario_id="scn", override=None):
        snap = snapshot()
        if override:
            snap.update(override)
        refs = {}
        for doc_type in ("network", "timetable", "transfer",
                         "service_window", "capacity", "zone"):
            status, payload = self.call("POST", "/documents", {
                "request_id": f"{req_prefix}-{doc_type}",
                "doc_type": doc_type, "doc_id": f"{doc_type}-main",
                "payload": snap[doc_type]})
            self.assertIn(status, (200, 201), payload)
            refs[doc_type] = [doc_type + "-main", 1]
        status, payload = self.call("POST", "/scenarios", {
            "request_id": f"{req_prefix}-scn", "title": "场景",
            "refs": refs, "params": PARAMS, "scenario_id": scenario_id})
        self.assertIn(status, (200, 201), payload)
        return refs

    def test_document_scenario_plan_review_compare_flow(self):
        self.bootstrap()
        self.seed_scenario()
        # 提交方案
        status, payload = self.call("POST", "/plans", {
            "request_id": "r-plan", "scenario_id": "scn", "name": "部门方案", "plan_id": "pln"})
        self.assertEqual(201, status)
        status, fetched = self.call("GET", "/plans/pln")
        self.assertEqual(200, status)
        self.assertEqual(1.0, fetched["result"]["summary"]["coverage_ratio"])
        # 列表过滤
        status, listed = self.call("GET", "/plans?scenario_id=scn&status=submitted")
        self.assertEqual(200, status)
        self.assertEqual(["pln"], [p["plan_id"] for p in listed["items"]])
        # 冻结校验
        status, verification = self.call("GET", "/plans/pln/verify")
        self.assertEqual(200, status)
        self.assertTrue(verification["matches"])
        # 审批
        status, payload = self.call("POST", "/reviews", {
            "request_id": "r-ap", "plan_id": "pln", "decision": "approve",
            "note": "同意"}, "rev1")
        self.assertEqual(201, status)
        status, reviews = self.call("GET", "/plans/pln/reviews")
        self.assertEqual(200, status)
        self.assertEqual(["approve"], [r["decision"] for r in reviews["items"]])
        # 对比自身对（gained/lost 均为空）
        status, comparison = self.call("POST", "/comparisons", {
            "request_id": "r-cmp", "plan_a": "pln", "plan_b": "pln"}, "rev1")
        self.assertEqual(200, status)
        self.assertEqual([], comparison["areas_lost"])
        status, loaded = self.call("GET", f"/comparisons/{comparison['comparison_id']}")
        self.assertEqual(200, status)
        self.assertEqual("pln", loaded["plan_a"])

    def test_new_version_only_affects_new_scenario(self):
        self.bootstrap()
        self.seed_scenario()
        self.call("POST", "/plans", {
            "request_id": "r-p1", "scenario_id": "scn", "name": "旧方案", "plan_id": "p1"})
        # 追加容量清零版本并冻结新场景
        self.call("POST", "/documents", {
            "request_id": "r-cap2", "doc_type": "capacity", "doc_id": "capacity-main",
            "payload": {"segments": [
                {"service_id": "sv-l3", "from_node": "Q", "to_node": "W", "remaining": 0}]}})
        refs = {doc_type: [f"{doc_type}-main", 1] for doc_type in
                ("network", "timetable", "transfer", "service_window", "zone")}
        refs["capacity"] = ["capacity-main", 2]
        status, payload = self.call("POST", "/scenarios", {
            "request_id": "r-scn2", "title": "新场景", "refs": refs,
            "params": PARAMS, "scenario_id": "scn2"})
        self.assertEqual(201, status, payload)
        self.call("POST", "/plans", {
            "request_id": "r-p2", "scenario_id": "scn2", "name": "新方案", "plan_id": "p2"})
        _, old = self.call("GET", "/plans/p1")
        _, new = self.call("GET", "/plans/p2")
        self.assertEqual(1.0, old["result"]["summary"]["coverage_ratio"])
        self.assertEqual(0.5, new["result"]["summary"]["coverage_ratio"])
        # 对比两个版本
        status, diff = self.call("POST", "/comparisons", {
            "request_id": "r-diff", "plan_a": "p1", "plan_b": "p2"}, "rev1")
        self.assertEqual(200, status)
        self.assertEqual(["a-q"], [l["area_id"] for l in diff["areas_lost"]])
        self.assertEqual("capacity", diff["areas_lost"][0]["primary_cause"])

    def test_permissions_enforced_over_http(self):
        self.bootstrap()
        # 未登录（未知操作者）
        status, payload = self.call("POST", "/documents", {
            "request_id": "r-x", "doc_type": "network", "doc_id": "nn",
            "payload": {"nodes": []}}, "nobody")
        self.assertEqual(404, status)
        self.seed_scenario()
        self.call("POST", "/plans", {
            "request_id": "r-p", "scenario_id": "scn", "name": "方案", "plan_id": "p"})
        # operator 不能批准
        status, payload = self.call("POST", "/reviews", {
            "request_id": "r-denied", "plan_id": "p", "decision": "approve"})
        self.assertEqual(403, status)

    def test_validation_errors_are_400(self):
        self.bootstrap()
        status, payload = self.call("POST", "/documents", {
            "request_id": "r-bad", "doc_type": "unknown", "doc_id": "x",
            "payload": {"a": 1}})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])
        status, payload = self.call("GET", "/plans/missing")
        self.assertEqual(404, status)
        status, payload = self.call("GET", "/documents/network/missing")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
