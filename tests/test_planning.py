import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from transport_coordination.planning import PlanningService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

from commute_fixture import PARAMS, snapshot


def docs():
    snap = snapshot()
    return {doc_type: snap[doc_type] for doc_type in
            ("network", "timetable", "transfer", "service_window", "capacity", "zone")}


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.planning = PlanningService(self.database, self.service, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="规划机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="规划师", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev", actor_id="a1", new_actor_id="rev1",
                                    display_name="审批人", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="aud", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _register_all(self, req="d", override=None):
        refs = {}
        for doc_type, payload in docs().items():
            if override and doc_type in override:
                payload = override[doc_type]
            receipt = self.planning.register_document(
                request_id=f"{req}-{doc_type}", actor_id="op1",
                doc_type=doc_type, doc_id=f"{doc_type}-main", payload=payload)
            refs[doc_type] = [receipt.resource_id.split("@")[0],
                              int(receipt.resource_id.split("@")[1])]
        return refs

    def test_documents_are_append_only_and_replayable(self):
        first = self.planning.register_document(
            request_id="doc-tt-1", actor_id="op1", doc_type="timetable",
            doc_id="tt", payload=docs()["timetable"])
        self.assertFalse(first.replayed)
        self.assertEqual(1, int(first.resource_id.split("@")[1]))
        replay = self.planning.register_document(
            request_id="doc-tt-1", actor_id="op1", doc_type="timetable",
            doc_id="tt", payload=docs()["timetable"])
        self.assertTrue(replay.replayed)
        changed = {"services": docs()["timetable"]["services"][:1]}
        second = self.planning.register_document(
            request_id="doc-tt-2", actor_id="op1", doc_type="timetable",
            doc_id="tt", payload=changed)
        self.assertEqual(2, int(second.resource_id.split("@")[1]))
        versions = self.planning.list_document_versions("timetable", "tt")
        self.assertEqual([1, 2], [v["version"] for v in versions])
        # 旧版本内容仍可读且未被改写
        self.assertEqual(docs()["timetable"], versions[0]["payload"])
        with self.assertRaises(ConflictError):
            self.planning.register_document(
                request_id="doc-tt-hack", actor_id="op1", doc_type="timetable",
                doc_id="tt", payload=changed, version=1)

    def test_invalid_doc_type_and_auditor_rejected(self):
        with self.assertRaises(ValidationError):
            self.planning.register_document(
                request_id="bad-type", actor_id="op1", doc_type="nope",
                doc_id="x", payload={"x": 1})
        with self.assertRaises(PermissionDenied):
            self.planning.register_document(
                request_id="aud-write", actor_id="au1", doc_type="network",
                doc_id="net", payload=docs()["network"])

    def test_scenario_freezes_versions_and_validates_consistency(self):
        refs = self._register_all()
        receipt = self.planning.create_scenario(
            request_id="scn-1", actor_id="op1", title="基准场景",
            refs=refs, params=PARAMS, scenario_id="scn-1")
        self.assertFalse(receipt.replayed)
        scenario = self.planning.get_scenario("scn-1")
        self.assertEqual(refs, scenario["refs"])
        # 引用缺失版本
        broken = dict(refs)
        broken["network"] = ["network-main", 99]
        with self.assertRaises(NotFoundError):
            self.planning.create_scenario(
                request_id="scn-bad", actor_id="op1", title="坏场景",
                refs=broken, params=PARAMS)
        # 内部不一致的输入组合无法冻结
        bad_refs = self._register_all(
            req="d2", override={"timetable": {"services": [
                {"service_id": "sv-ghost", "line": "ZZ", "stops": [
                    {"node_id": "A", "departure": "07:00"},
                    {"node_id": "W", "arrival": "07:10"}]}]}})
        with self.assertRaises(ValidationError):
            self.planning.create_scenario(
                request_id="scn-invalid", actor_id="op1", title="不一致场景",
                refs=bad_refs, params=PARAMS)

    def test_plan_result_is_frozen_and_immune_to_new_versions(self):
        refs = self._register_all()
        self.planning.create_scenario(request_id="scn", actor_id="op1",
                                      title="场景", refs=refs, params=PARAMS,
                                      scenario_id="scn")
        plan = self.planning.submit_plan(request_id="pln", actor_id="op1",
                                         scenario_id="scn", name="部门方案", plan_id="pln")
        self.assertEqual("submitted", self.planning.get_plan("pln")["status"])
        ratio_before = self.planning.get_plan("pln")["result"]["summary"]["coverage_ratio"]
        self.assertEqual(1.0, ratio_before)
        # 冻结校验通过
        self.assertTrue(self.planning.verify_plan("pln")["matches"])
        # 之后追加的新版本不能影响旧场景/旧方案
        self.planning.register_document(
            request_id="tt-suspended", actor_id="op1", doc_type="timetable",
            doc_id="timetable-main",
            payload={"services": docs()["timetable"]["services"][:3]})  # L3 被移除
        self.assertEqual(
            1.0, self.planning.get_plan("pln")["result"]["summary"]["coverage_ratio"])
        self.assertTrue(self.planning.verify_plan("pln")["matches"])
        self.assertFalse(plan.replayed)

    def test_tampered_plan_fails_verification(self):
        refs = self._register_all()
        self.planning.create_scenario(request_id="scn", actor_id="op1", title="场景",
                                      refs=refs, params=PARAMS, scenario_id="scn")
        self.planning.submit_plan(request_id="pln", actor_id="op1", scenario_id="scn",
                                  name="方案", plan_id="pln")
        self.database.connection.execute(
            "UPDATE plans SET result_json=? WHERE plan_id=?", ('{"tampered": true}', "pln"))
        verification = self.planning.verify_plan("pln")
        self.assertFalse(verification["matches"])

    def test_only_one_approved_plan_and_supersede_flow(self):
        refs = self._register_all()
        self.planning.create_scenario(request_id="scn", actor_id="op1", title="场景",
                                      refs=refs, params=PARAMS, scenario_id="scn")
        self.planning.submit_plan(request_id="pa", actor_id="op1", scenario_id="scn",
                                  name="甲方案", plan_id="pa")
        self.planning.submit_plan(request_id="pb", actor_id="rev1", scenario_id="scn",
                                  name="乙方案", plan_id="pb")
        self.planning.review_plan(request_id="ap-pa", actor_id="rev1", plan_id="pa",
                                  decision="approve", note="同意甲")
        with self.assertRaises(ConflictError):
            self.planning.review_plan(request_id="ap-pb", actor_id="rev1",
                                      plan_id="pb", decision="approve")
        # operator 不能审批
        with self.assertRaises(PermissionDenied):
            self.planning.review_plan(request_id="ap-op", actor_id="op1",
                                      plan_id="pb", decision="approve")
        # reviewer 不能接替，admin 可以
        with self.assertRaises(PermissionDenied):
            self.planning.review_plan(request_id="su-rev", actor_id="rev1",
                                      plan_id="pb", decision="supersede")
        self.planning.review_plan(request_id="su-admin", actor_id="a1", plan_id="pb",
                                  decision="supersede", note="改用乙")
        statuses = {p["plan_id"]: p["status"]
                    for p in self.planning.list_plans(scenario_id="scn")}
        self.assertEqual({"pa": "superseded", "pb": "approved"}, statuses)
        old_reviews = [r["decision"] for r in self.planning.list_reviews("pa")]
        self.assertEqual(["approve", "superseded"], old_reviews)
        # 已获批不能重复审批，也不能直接驳回
        with self.assertRaises(ConflictError):
            self.planning.review_plan(request_id="ap-pb-again", actor_id="a1",
                                      plan_id="pb", decision="approve")
        with self.assertRaises(ConflictError):
            self.planning.review_plan(request_id="rj-pb", actor_id="a1",
                                      plan_id="pb", decision="reject")
        # 部门过滤
        self.assertEqual(
            ["pa", "pb"],
            [p["plan_id"] for p in self.planning.list_plans(department_org="o1")])

    def test_reject_and_comment_keep_review_history(self):
        refs = self._register_all()
        self.planning.create_scenario(request_id="scn", actor_id="op1", title="场景",
                                      refs=refs, params=PARAMS, scenario_id="scn")
        self.planning.submit_plan(request_id="pa", actor_id="op1", scenario_id="scn",
                                  name="方案", plan_id="pa")
        self.planning.review_plan(request_id="cmt", actor_id="rev1", plan_id="pa",
                                  decision="comment", note="请补充容量依据")
        self.assertEqual("submitted", self.planning.get_plan("pa")["status"])
        self.planning.review_plan(request_id="rej", actor_id="rev1", plan_id="pa",
                                  decision="reject", note="数据不足")
        self.assertEqual("rejected", self.planning.get_plan("pa")["status"])
        decisions = [r["decision"] for r in self.planning.list_reviews("pa")]
        self.assertEqual(["comment", "reject"], decisions)

    def test_comparison_is_persisted_and_idempotent_by_pair(self):
        refs = self._register_all()
        self.planning.create_scenario(request_id="s1", actor_id="op1", title="旧",
                                      refs=refs, params=PARAMS, scenario_id="s1")
        new_refs = dict(refs)
        self.planning.register_document(
            request_id="cap-zero", actor_id="op1", doc_type="capacity",
            doc_id="capacity-main",
            payload={"segments": [{"service_id": "sv-l3", "from_node": "Q",
                                   "to_node": "W", "remaining": 0}]})
        new_refs["capacity"] = ["capacity-main", 2]
        self.planning.create_scenario(request_id="s2", actor_id="op1", title="新",
                                      refs=new_refs, params=PARAMS, scenario_id="s2")
        self.planning.submit_plan(request_id="p1", actor_id="op1", scenario_id="s1",
                                  name="旧", plan_id="p1")
        self.planning.submit_plan(request_id="p2", actor_id="op1", scenario_id="s2",
                                  name="新", plan_id="p2")
        first = self.planning.compare(request_id="cmp-1", actor_id="rev1",
                                      plan_a="p1", plan_b="p2")
        again = self.planning.compare(request_id="cmp-other", actor_id="rev1",
                                      plan_a="p1", plan_b="p2")
        self.assertEqual(first["comparison_id"], again["comparison_id"])
        loaded = self.planning.get_comparison(first["comparison_id"])
        self.assertEqual("p1", loaded["plan_a"])
        with self.assertRaises(NotFoundError):
            self.planning.get_comparison("nope")

    def test_review_resumes_after_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            clock = FixedClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
            service = DomainService(database, clock)
            planning = PlanningService(database, service, clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="规划机构")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="规划师", role="operator", organization_id="o1")
            service.register_actor(request_id="rev", actor_id="a1", new_actor_id="rev1",
                                   display_name="审批人", role="reviewer", organization_id="o1")
            refs = {}
            for doc_type, payload in docs().items():
                receipt = planning.register_document(
                    request_id=f"d-{doc_type}", actor_id="op1",
                    doc_type=doc_type, doc_id=f"{doc_type}-main", payload=payload)
                refs[doc_type] = [doc_type + "-main", 1]
            planning.create_scenario(request_id="scn", actor_id="op1", title="场景",
                                     refs=refs, params=PARAMS, scenario_id="scn")
            planning.submit_plan(request_id="pln", actor_id="op1", scenario_id="scn",
                                 name="待批方案", plan_id="pln")
            database.close()

            database2 = Database(path)
            service2 = DomainService(database2, clock)
            planning2 = PlanningService(database2, service2, clock)
            self.assertEqual("submitted", planning2.get_plan("pln")["status"])
            planning2.review_plan(request_id="ap", actor_id="rev1", plan_id="pln",
                                  decision="approve", note="重启后完成审批")
            self.assertEqual("approved", planning2.get_plan("pln")["status"])
            self.assertTrue(planning2.verify_plan("pln")["matches"])
            valid, _ = service2.verify_audit()
            self.assertTrue(valid)
            database2.close()


if __name__ == "__main__":
    unittest.main()
