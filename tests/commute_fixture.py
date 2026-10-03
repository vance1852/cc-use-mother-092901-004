"""通勤测试共用的小型网络夹具。

线网：A --L1(20)--> H1 ==换乘8分==> H2 --L2(20)--> W ；Q --L3(30)--> W。
W 是就业片区，A/Q 是两个居住片区，出发批次 07:00。
"""

from __future__ import annotations

from typing import Any


PARAMS = {"deadline": "09:00", "commute_limit": 60, "capacity_threshold": 1,
          "batches": ["07:00"]}


def network_doc() -> dict[str, Any]:
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


def timetable_doc(*, early_connection: bool = False, suspend_l3: bool = False) -> dict[str, Any]:
    # 默认接驳：H2 07:30 发车，07:28 走到站台可赶上；提前到 07:25 则错位
    connection_dep = "07:25" if early_connection else "07:30"
    connection_arr = "07:45" if early_connection else "07:50"
    services = [
        {"service_id": "sv-l1", "line": "L1", "stops": [
            {"node_id": "A", "departure": "07:00"},
            {"node_id": "H1", "arrival": "07:20"}]},
        {"service_id": "sv-l2-early", "line": "L2", "stops": [
            {"node_id": "H2", "departure": connection_dep},
            {"node_id": "W", "arrival": connection_arr}]},
        {"service_id": "sv-l2-late", "line": "L2", "stops": [
            {"node_id": "H2", "departure": "08:30"},
            {"node_id": "W", "arrival": "08:50"}]},
    ]
    if not suspend_l3:
        services.append({"service_id": "sv-l3", "line": "L3", "stops": [
            {"node_id": "Q", "departure": "07:10"},
            {"node_id": "W", "arrival": "07:40"}]})
    return {"services": services}


def transfer_doc(*, minutes: int = 8) -> dict[str, Any]:
    return {"transfers": [{"from_node": "H1", "to_node": "H2", "minutes": minutes},
                          {"node_id": "H2", "minutes": 0}]}


def window_doc(*, block_early: bool = False) -> dict[str, Any]:
    if not block_early:
        return {"windows": []}
    return {"windows": [
        {"service_id": "sv-l2-early", "node_id": "H2",
         "first_departure": "07:31", "last_departure": "08:00"},
    ]}


def capacity_doc(*, l3_remaining: int | None = 100) -> dict[str, Any]:
    segments = []
    if l3_remaining is not None:
        segments.append({"service_id": "sv-l3", "from_node": "Q",
                         "to_node": "W", "remaining": l3_remaining})
    return {"segments": segments}


def zone_doc() -> dict[str, Any]:
    return {
        "employment_zones": [
            {"zone_id": "z-w", "name": "中心就业片区", "node_ids": ["W"]},
        ],
        "residential_areas": [
            {"area_id": "a-a", "name": "A 居住片", "node_ids": ["A"]},
            {"area_id": "a-q", "name": "Q 居住片", "node_ids": ["Q"]},
        ],
    }


def snapshot(*, early_connection: bool = False, suspend_l3: bool = False,
             transfer_minutes: int = 8, block_early_window: bool = False,
             l3_remaining: int | None = 100) -> dict[str, dict[str, Any]]:
    return {
        "network": network_doc(),
        "timetable": timetable_doc(early_connection=early_connection, suspend_l3=suspend_l3),
        "transfer": transfer_doc(minutes=transfer_minutes),
        "service_window": window_doc(block_early=block_early_window),
        "capacity": capacity_doc(l3_remaining=None if suspend_l3 else l3_remaining),
        "zone": zone_doc(),
    }


def area(result: dict[str, Any], area_id: str) -> dict[str, Any]:
    return next(a for a in result["areas"] if a["area_id"] == area_id)
