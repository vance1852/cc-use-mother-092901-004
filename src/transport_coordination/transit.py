"""通勤可达性引擎：时刻搜索、容量/换乘/首末班约束与反事实归因。

本模块只处理纯数据：输入为冻结后的文档快照，输出为可直接冻结的 JSON 兼容
字典。搜索采用基于绝对分钟时间的最早到达标号扩展（时间依赖网络上的
Dijkstra），路径上的等待、换乘与每个区间的拥挤余量都会逐条记录。

对未覆盖区域使用三组反事实搜索归因：
- wait     ：忽略班次对齐与首末班窗口，假设到站即可上车（旅行时长仍按图定）；
- capacity ：忽略区间拥挤余量门槛；
- transfer ：所有换乘通道耗时归零并允许双向使用既有通道。

多种约束单独放松都可恢复覆盖时（典型如接驳错位：通道慢几分钟恰好错过
提前发车的列车），按 5 分钟到达桶归为并列，并优先报告更贴近真实运营的
干预：capacity（加车/扩容）> transfer（缩短换乘）> wait（让列车等人），
因为前两者不改变图定时刻。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError

RESULT_SCHEMA = "commute-coverage/1"

DOC_TYPES = ("network", "timetable", "transfer", "service_window", "capacity", "zone")


# ---------------------------------------------------------------------------
# 时间与基础校验
# ---------------------------------------------------------------------------

def to_minutes(value: Any, field_name: str) -> int:
    """把整数分钟或 ``HH:MM`` 文本统一为零点起算的分钟数。"""

    if isinstance(value, bool):
        raise ValidationError(f"{field_name} 时间格式无效")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str) and ":" in value:
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValidationError(f"{field_name} 时间格式无效")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValidationError(f"{field_name} 时间超出范围")
        minutes = hour * 60 + minute
    else:
        raise ValidationError(f"{field_name} 时间格式无效")
    if not 0 <= minutes <= 24 * 60:
        raise ValidationError(f"{field_name} 时间超出范围")
    return minutes


def hhmm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} 必须是对象")
    return value


def _sequence(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} 必须是数组")
    return value


# ---------------------------------------------------------------------------
# 规范化后的网络结构
# ---------------------------------------------------------------------------

@dataclass
class Service:
    service_id: str
    line: str
    stop_nodes: list[str] = field(default_factory=list)
    departures: list[int] = field(default_factory=list)
    arrivals: list[int] = field(default_factory=list)
    first_window: dict[str, int] = field(default_factory=dict)
    last_window: dict[str, int] = field(default_factory=dict)


@dataclass
class TransitNetwork:
    nodes: dict[str, str]
    edges: dict[str, dict[str, Any]]
    services: dict[str, Service]
    services_by_node: dict[str, list[tuple[str, int]]]
    transfers: dict[tuple[str, str], int]
    transfer_minutes_self: dict[str, int]
    capacities: dict[tuple[str, str, str], int]
    line_edge: dict[tuple[str, str, str], str]

    def edge_id(self, line: str, from_node: str, to_node: str) -> str | None:
        return self.line_edge.get((line, from_node, to_node))

    def transfer_time(self, from_node: str, to_node: str) -> int | None:
        if from_node == to_node:
            return self.transfer_minutes_self.get(from_node, 0)
        return self.transfers.get((from_node, to_node))

    def remaining(self, service_id: str, from_node: str, to_node: str) -> int | None:
        return self.capacities.get((service_id, from_node, to_node))


def build_network(snapshot: dict[str, dict[str, Any]]) -> TransitNetwork:
    """从冻结快照校验并组装网络；任何不一致都抛出 ValidationError。"""

    for doc_type in DOC_TYPES:
        _require(doc_type in snapshot, f"场景缺少 {doc_type} 输入")
        _mapping(snapshot[doc_type], doc_type)

    # ---- 拓扑 ----
    network = _mapping(snapshot["network"], "network")
    nodes: dict[str, str] = {}
    for item in _sequence(network.get("nodes"), "network.nodes"):
        item = _mapping(item, "network.nodes[]")
        node_id = str(item.get("node_id", "")).strip()
        _require(bool(node_id), "node_id 不能为空")
        _require(node_id not in nodes, f"节点 {node_id} 重复")
        nodes[node_id] = str(item.get("name", node_id))
    _require(nodes, "network.nodes 至少需要一个节点")

    edges: dict[str, dict[str, Any]] = {}
    edge_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    line_edge: dict[tuple[str, str, str], str] = {}
    for item in _sequence(network.get("edges"), "network.edges"):
        item = _mapping(item, "network.edges[]")
        edge_id = str(item.get("edge_id", "")).strip()
        line = str(item.get("line", "")).strip()
        from_node = str(item.get("from_node", "")).strip()
        to_node = str(item.get("to_node", "")).strip()
        travel = item.get("travel_minutes")
        _require(bool(edge_id), "edge_id 不能为空")
        _require(bool(line), "edge.line 不能为空")
        _require(edge_id not in edges, f"边 {edge_id} 重复")
        _require(from_node in nodes and to_node in nodes, f"边 {edge_id} 引用了不存在的节点")
        _require(from_node != to_node, f"边 {edge_id} 不能自环")
        _require(isinstance(travel, int) and not isinstance(travel, bool) and travel > 0,
                 f"边 {edge_id} 的 travel_minutes 必须是正整数")
        edge = {"edge_id": edge_id, "line": line, "from_node": from_node,
                "to_node": to_node, "travel_minutes": travel}
        edges[edge_id] = edge
        edge_pairs[(from_node, to_node)] = edge
        line_edge[(line, from_node, to_node)] = edge_id

    # ---- 班次 ----
    timetable = _mapping(snapshot["timetable"], "timetable")
    services: dict[str, Service] = {}
    services_by_node: dict[str, list[tuple[str, int]]] = {node: [] for node in nodes}
    for item in _sequence(timetable.get("services"), "timetable.services"):
        item = _mapping(item, "timetable.services[]")
        service_id = str(item.get("service_id", "")).strip()
        line = str(item.get("line", "")).strip()
        _require(bool(service_id), "service_id 不能为空")
        _require(service_id not in services, f"班次 {service_id} 重复")
        _require(bool(line), f"班次 {service_id} 的 line 不能为空")
        stops_raw = _sequence(item.get("stops"), f"service {service_id}.stops")
        _require(len(stops_raw) >= 2, f"班次 {service_id} 至少需要两个停靠点")
        stop_nodes: list[str] = []
        departures: list[int] = []
        arrivals: list[int] = []
        previous_time = -1
        for index, raw in enumerate(stops_raw):
            stop = _mapping(raw, f"service {service_id}.stops[]")
            node_id = str(stop.get("node_id", "")).strip()
            _require(node_id in nodes, f"班次 {service_id} 停靠了不存在的节点 {node_id}")
            dep = stop.get("departure")
            arr = stop.get("arrival")
            dep_m = to_minutes(dep, f"service {service_id} {node_id} departure") if dep is not None else None
            arr_m = to_minutes(arr, f"service {service_id} {node_id} arrival") if arr is not None else None
            if index == 0:
                _require(dep_m is not None, f"班次 {service_id} 首站必须给出 departure")
                arr_m = arr_m if arr_m is not None else dep_m
            if index == len(stops_raw) - 1:
                _require(arr_m is not None, f"班次 {service_id} 末站必须给出 arrival")
                dep_m = dep_m if dep_m is not None else arr_m
            if dep_m is None:
                dep_m = arr_m
            if arr_m is None:
                arr_m = dep_m
            _require(dep_m >= previous_time, f"班次 {service_id} 时刻不是单调递增")
            if index > 0:
                edge = edge_pairs.get((stop_nodes[-1], node_id))
                _require(edge is not None,
                         f"班次 {service_id} 的相邻停靠 {stop_nodes[-1]}->{node_id} 缺少同方向网络边")
                _require(edge["line"] == line,
                         f"班次 {service_id} 的线路与边 {edge['edge_id']} 不一致")
                _require(arr_m - departures[-1] == edge["travel_minutes"],
                         f"班次 {service_id} 在边 {edge['edge_id']} 上的运行时长与拓扑不一致")
            stop_nodes.append(node_id)
            departures.append(dep_m)
            arrivals.append(arr_m)
            previous_time = dep_m
        service = Service(service_id, line, stop_nodes, departures, arrivals)
        services[service_id] = service
        seen: set[str] = set()
        for index, node_id in enumerate(stop_nodes):
            _require(node_id not in seen, f"班次 {service_id} 在 {node_id} 重复停靠")
            seen.add(node_id)
            if index < len(stop_nodes) - 1:
                services_by_node[node_id].append((service_id, index))
            service.first_window[node_id] = departures[index]
            service.last_window[node_id] = departures[index]

    # ---- 首末班窗口覆盖 ----
    windows = _mapping(snapshot["service_window"], "service_window")
    for item in _sequence(windows.get("windows", []), "service_window.windows"):
        item = _mapping(item, "service_window.windows[]")
        service_id = str(item.get("service_id", "")).strip()
        node_id = str(item.get("node_id", "")).strip()
        _require(service_id in services, f"首末班窗口引用了未知班次 {service_id}")
        service = services[service_id]
        _require(node_id in service.stop_nodes, f"首末班窗口引用了 {service_id} 不停靠的节点 {node_id}")
        first_m = to_minutes(item["first_departure"], f"{service_id}/{node_id} first_departure")
        last_m = to_minutes(item["last_departure"], f"{service_id}/{node_id} last_departure")
        _require(first_m <= last_m, f"{service_id}/{node_id} 首末班窗口倒置")
        service.first_window[node_id] = first_m
        service.last_window[node_id] = last_m

    # ---- 换乘 ----
    transfer_doc = _mapping(snapshot["transfer"], "transfer")
    transfers: dict[tuple[str, str], int] = {}
    transfer_minutes_self: dict[str, int] = {}
    for item in _sequence(transfer_doc.get("transfers", []), "transfer.transfers"):
        item = _mapping(item, "transfer.transfers[]")
        minutes = item.get("minutes")
        _require(isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0,
                 "换乘 minutes 必须是非负整数")
        if "node_id" in item:
            node_id = str(item.get("node_id", "")).strip()
            _require(node_id in nodes, f"换乘记录引用了未知节点 {node_id}")
            transfer_minutes_self[node_id] = minutes
        else:
            from_node = str(item.get("from_node", "")).strip()
            to_node = str(item.get("to_node", "")).strip()
            _require(from_node in nodes and to_node in nodes, "换乘记录引用了未知节点")
            _require(from_node != to_node, "同站换乘请使用 node_id 形式")
            transfers[(from_node, to_node)] = minutes

    # ---- 容量 ----
    capacity_doc = _mapping(snapshot["capacity"], "capacity")
    capacities: dict[tuple[str, str, str], int] = {}
    for item in _sequence(capacity_doc.get("segments", []), "capacity.segments"):
        item = _mapping(item, "capacity.segments[]")
        service_id = str(item.get("service_id", "")).strip()
        from_node = str(item.get("from_node", "")).strip()
        to_node = str(item.get("to_node", "")).strip()
        remaining = item.get("remaining")
        _require(service_id in services, f"容量记录引用了未知班次 {service_id}")
        _require(isinstance(remaining, int) and not isinstance(remaining, bool) and remaining >= 0,
                 f"{service_id} 容量 remaining 必须是非负整数")
        stop_nodes = services[service_id].stop_nodes
        _require(from_node in stop_nodes and to_node in stop_nodes
                 and stop_nodes.index(to_node) > stop_nodes.index(from_node),
                 f"容量区间 {service_id} {from_node}->{to_node} 不是顺向可达停靠段")
        capacities[(service_id, from_node, to_node)] = remaining

    return TransitNetwork(nodes, edges, services, services_by_node, transfers,
                          transfer_minutes_self, capacities, line_edge)


# ---------------------------------------------------------------------------
# 最早到达搜索
# ---------------------------------------------------------------------------

WALK = "w"   # 在站台/通道上
RIDE = "r"   # 在班次上，刚到达某停靠节点


@dataclass
class SearchResult:
    labels: dict[Any, int]
    pred: dict[Any, tuple[Any, dict[str, Any]]]

    def arrival(self, node: str) -> int | None:
        values = [self.labels[s] for s in ((WALK, node), (RIDE, node)) if s in self.labels]
        return min(values) if values else None


def earliest_arrival(network: TransitNetwork, starts: dict[str, int], *,
                     relax_capacity: bool = False,
                     relax_wait: bool = False,
                     relax_transfer: bool = False,
                     capacity_threshold: int = 1) -> SearchResult:
    """从若干起点节点的给定时刻出发求全网络最早到达标号。"""

    labels: dict[Any, int] = {}
    pred: dict[Any, tuple[Any, dict[str, Any]]] = {}
    heap: list[tuple[int, int, Any]] = []
    counter = 0

    def push(state: Any, time_value: int, previous: Any | None, leg: dict[str, Any] | None) -> None:
        old = labels.get(state)
        if old is not None and old <= time_value:
            return
        labels[state] = time_value
        if previous is not None and leg is not None:
            pred[state] = (previous, leg)
        nonlocal counter
        heapq.heappush(heap, (time_value, counter, state))
        counter += 1

    for node, start_time in starts.items():
        if node in network.nodes:
            push((WALK, node), start_time, None, None)

    while heap:
        time_value, _, state = heapq.heappop(heap)
        if labels.get(state) != time_value:
            continue
        kind, node = state[0], state[1]

        if kind == RIDE:
            service_id = state[2]
            service = network.services[service_id]
            index = service.stop_nodes.index(node)
            self_transfer = network.transfer_minutes_self.get(node, 0)
            # 下车（同站换乘按配置耗时走到接驳站台）
            push((WALK, node), time_value + self_transfer, state,
                 {"leg_type": "alight", "service_id": service_id, "node_id": node,
                  "time": time_value, "self_transfer_minutes": self_transfer})
            # 继续乘坐到下一站
            if index + 1 < len(service.stop_nodes):
                next_node = service.stop_nodes[index + 1]
                arrive = service.arrivals[index + 1] + (time_value - service.arrivals[index])
                remaining = network.remaining(service_id, node, next_node)
                capacity_ok = relax_capacity or remaining is None or remaining >= capacity_threshold
                if capacity_ok and arrive >= time_value:
                    push((RIDE, next_node, service_id), arrive, state,
                         {"leg_type": "ride", "service_id": service_id, "line": service.line,
                          "from_node": node, "to_node": next_node,
                          "sched_departure": service.departures[index],
                          "sched_arrival": service.arrivals[index + 1],
                          "edge_id": network.edge_id(service.line, node, next_node),
                          "remaining": remaining})
            continue

        # kind == WALK：站间换乘通道
        for (from_node, to_node), minutes in network.transfers.items():
            if from_node != node:
                continue
            applied = 0 if relax_transfer else minutes
            push((WALK, to_node), time_value + applied, state,
                 {"leg_type": "transfer", "from_node": from_node, "to_node": to_node,
                  "minutes": minutes, "applied_minutes": applied, "reversed": False})
        if relax_transfer:
            # 反事实：既有通道双向可用
            for (from_node, to_node), minutes in network.transfers.items():
                if to_node == node:
                    push((WALK, from_node), time_value, state,
                         {"leg_type": "transfer", "from_node": to_node, "to_node": from_node,
                          "minutes": minutes, "applied_minutes": 0, "reversed": True})

        # 登乘所有可乘班次
        for service_id, index in network.services_by_node.get(node, []):
            service = network.services[service_id]
            scheduled_dep = service.departures[index]
            wait = scheduled_dep - time_value
            if not relax_wait and wait < 0:
                continue  # 该班次已发车
            if not relax_wait and not (service.first_window[node] <= scheduled_dep <= service.last_window[node]):
                continue  # 落在首末班窗口之外
            board_time = time_value if relax_wait else scheduled_dep
            next_node = service.stop_nodes[index + 1]
            sched_arrival = service.arrivals[index + 1]
            arrive = sched_arrival + (board_time - scheduled_dep)
            remaining = network.remaining(service_id, node, next_node)
            if not (relax_capacity or remaining is None or remaining >= capacity_threshold):
                continue
            push((RIDE, next_node, service_id), arrive, state,
                 {"leg_type": "board", "service_id": service_id, "line": service.line,
                  "from_node": node, "to_node": next_node,
                  "sched_departure": scheduled_dep, "board_time": board_time,
                  "wait_minutes": max(wait, 0), "missed": wait < 0,
                  "sched_arrival": sched_arrival,
                  "edge_id": network.edge_id(service.line, node, next_node),
                  "remaining": remaining,
                  "window_first": service.first_window[node],
                  "window_last": service.last_window[node]})

    return SearchResult(labels, pred)


def reconstruct_path(network: TransitNetwork, result: SearchResult, node: str) -> list[dict[str, Any]] | None:
    """把标号前驱链还原为 start/ride/transfer 三类腿。"""

    candidates = [s for s in ((WALK, node), (RIDE, node)) if s in result.labels]
    if not candidates:
        return None
    state = min(candidates, key=lambda s: result.labels[s])

    raw: list[dict[str, Any]] = []
    current: Any = state
    while current in result.pred:
        previous, leg = result.pred[current]
        raw.append(leg)
        current = previous
    raw.reverse()

    legs: list[dict[str, Any]] = []
    index = 0
    while index < len(raw):
        leg = raw[index]
        if leg["leg_type"] == "board":
            ride_legs: list[dict[str, Any]] = []
            index += 1
            while index < len(raw) and raw[index]["leg_type"] == "ride":
                ride_legs.append(raw[index])
                index += 1
            last = ride_legs[-1] if ride_legs else leg
            service_id = leg["service_id"]
            service = network.services[service_id]
            i_from = service.stop_nodes.index(leg["from_node"])
            i_to = service.stop_nodes.index(last["to_node"])
            shift = leg["board_time"] - leg["sched_departure"]
            segments = []
            for i in range(i_from, i_to):
                fn, tn = service.stop_nodes[i], service.stop_nodes[i + 1]
                segments.append({
                    "from_node": fn, "to_node": tn,
                    "edge_id": network.edge_id(service.line, fn, tn),
                    "sched_departure": service.departures[i] + shift,
                    "sched_arrival": service.arrivals[i + 1] + shift,
                    "travel_minutes": service.arrivals[i + 1] - service.departures[i],
                    "remaining": network.remaining(service_id, fn, tn),
                })
            legs.append({
                "leg_type": "ride",
                "service_id": service_id,
                "line": service.line,
                "from_node": leg["from_node"],
                "to_node": last["to_node"],
                "board_time": leg["board_time"],
                "board_time_hhmm": hhmm(leg["board_time"]),
                "arrive_time": segments[-1]["sched_arrival"],
                "arrive_time_hhmm": hhmm(segments[-1]["sched_arrival"]),
                "wait_minutes": leg["wait_minutes"],
                "travel_minutes": segments[-1]["sched_arrival"] - leg["board_time"] - leg["wait_minutes"],
                "total_minutes": segments[-1]["sched_arrival"] - leg["board_time"],
                "window_first_hhmm": hhmm(leg["window_first"]),
                "window_last_hhmm": hhmm(leg["window_last"]),
                "segments": segments,
                "binding_remaining": min((s["remaining"] for s in segments if s["remaining"] is not None),
                                         default=None),
            })
            continue
        if leg["leg_type"] == "alight":
            if leg.get("self_transfer_minutes"):
                legs.append({"leg_type": "transfer", "from_node": leg["node_id"], "to_node": leg["node_id"],
                             "minutes": leg["self_transfer_minutes"], "reversed": False,
                             "connect_wait_minutes": None})
            index += 1
            continue
        if leg["leg_type"] == "transfer":
            legs.append({"leg_type": "transfer", "from_node": leg["from_node"], "to_node": leg["to_node"],
                         "minutes": leg["applied_minutes"], "reversed": leg.get("reversed", False),
                         "connect_wait_minutes": None})
            index += 1
            continue
        index += 1  # 孤立 ride（理论上不会出现）

    # 换乘腿补充接续等待
    for i, leg in enumerate(legs):
        if leg["leg_type"] == "transfer":
            following = next((l for l in legs[i + 1:] if l["leg_type"] == "ride"), None)
            if following is not None:
                leg["connect_wait_minutes"] = following["wait_minutes"]
                leg["arrive_time_hhmm"] = hhmm(following["board_time"] - following["wait_minutes"])

    # 起点腿
    first_ride = next((l for l in legs if l["leg_type"] == "ride"), None)
    start_node = first_ride["from_node"] if first_ride else node
    start_times = [t for s, t in result.labels.items() if s == (WALK, start_node)]
    start_time = min(start_times) if start_times else None
    legs.insert(0, {"leg_type": "start", "node_id": start_node, "time": start_time,
                    "time_hhmm": hhmm(start_time)})
    return legs


# ---------------------------------------------------------------------------
# 评估与归因
# ---------------------------------------------------------------------------

RELAX_MODES = (
    ("wait", {"relax_wait": True}),
    ("capacity", {"relax_capacity": True}),
    ("transfer", {"relax_transfer": True}),
)

MODE_TEXT = {
    "wait": "到站后等待时间过长或受首末班窗口限制，错过接驳班次",
    "capacity": "区间拥挤余量低于容量门槛，无法按计划乘降",
    "transfer": "站内/站间换乘耗时导致错过衔接班次",
}

# 到达时刻相差不超过一个桶（5 分钟）视为并列，按运营干预的现实性排序
CAUSE_TIE_BUCKET = 5
CAUSE_PREFERENCE = {"capacity": 0, "transfer": 1, "wait": 2}


def _feasible(arrival: int | None, start: int, deadline: int, commute_limit: int) -> bool:
    return (arrival is not None
            and arrival <= deadline
            and arrival - start <= commute_limit)


def _pick(search: SearchResult, zones: dict[str, list[str]], zone_deadline: dict[str, int],
          start: int, commute_limit: int, fallback_deadline: int) -> dict[str, Any] | None:
    """在搜索结果中选出可行的最早到达（不可行时返回最近到达）。"""

    feasible: dict[str, Any] | None = None
    nearest: dict[str, Any] | None = None
    for zone_id, z_nodes in zones.items():
        zone_end = zone_deadline.get(zone_id, fallback_deadline)
        for z_node in z_nodes:
            arrival = search.arrival(z_node)
            if arrival is None:
                continue
            candidate = {"arrival": arrival, "zone_id": zone_id, "node_id": z_node, "zone_end": zone_end}
            if nearest is None or arrival < nearest["arrival"]:
                nearest = candidate
            if _feasible(arrival, start, zone_end, commute_limit) and (
                    feasible is None or arrival < feasible["arrival"]):
                feasible = candidate
    return feasible or nearest


def _cause_detail(network: TransitNetwork, mode: str, base: SearchResult,
                  relaxed: SearchResult, target_node: str, threshold: int) -> dict[str, Any]:
    path = reconstruct_path(network, relaxed, target_node) or []
    if mode == "wait":
        # 用真实图定发车时刻对照基线搜索的站台到达时刻，定位错过的接驳
        worst = None
        for leg in path:
            if leg["leg_type"] != "ride":
                continue
            service = network.services[leg["service_id"]]
            index = service.stop_nodes.index(leg["from_node"])
            true_dep = service.departures[index]
            base_platform = base.labels.get((WALK, leg["from_node"]))
            entry = {"service_id": leg["service_id"], "line": leg["line"],
                     "node_id": leg["from_node"],
                     "sched_departure_hhmm": hhmm(true_dep),
                     "arrive_at_platform_hhmm": hhmm(base_platform),
                     "missed_connection": base_platform is not None and base_platform > true_dep,
                     "wait_minutes": (max(true_dep - base_platform, 0)
                                      if base_platform is not None else None)}
            key = (entry["missed_connection"], entry["wait_minutes"] if entry["wait_minutes"] is not None else -1)
            if worst is None or key > (worst["missed_connection"],
                                       worst["wait_minutes"] if worst["wait_minutes"] is not None else -1):
                worst = entry
        return {"summary": MODE_TEXT["wait"], "wait_at": worst}
    if mode == "capacity":
        tight = None
        for leg in path:
            if leg["leg_type"] != "ride":
                continue
            for segment in leg["segments"]:
                remaining = segment["remaining"]
                if remaining is None:
                    continue
                if tight is None or remaining < tight["remaining"]:
                    tight = {"service_id": leg["service_id"], "line": leg["line"],
                             "from_node": segment["from_node"], "to_node": segment["to_node"],
                             "edge_id": segment["edge_id"], "remaining": remaining,
                             "threshold": threshold, "below_threshold": remaining < threshold}
        return {"summary": MODE_TEXT["capacity"], "blocking_segment": tight}
    # transfer：用配置中的真实换乘耗时对照后序列车的图定发车时刻
    used = None
    for position, leg in enumerate(path):
        if leg["leg_type"] != "transfer":
            continue
        configured = network.transfer_time(leg["from_node"], leg["to_node"])
        following = next((l for l in path[position + 1:] if l["leg_type"] == "ride"), None)
        miss_info = None
        if following is not None:
            service = network.services[following["service_id"]]
            true_dep = service.departures[service.stop_nodes.index(following["from_node"])]
            base_platform = base.labels.get((WALK, leg["to_node"]))
            miss_info = {"connect_service_id": following["service_id"],
                         "connect_departure_hhmm": hhmm(true_dep),
                         "base_platform_hhmm": hhmm(base_platform),
                         "missed_connection": base_platform is not None and base_platform > true_dep}
        candidate = {"from_node": leg["from_node"], "to_node": leg["to_node"],
                     "minutes": configured if configured is not None else leg["minutes"],
                     "connection": miss_info}
        if used is None or (candidate["minutes"] or 0) > (used["minutes"] or 0):
            used = candidate
    return {"summary": MODE_TEXT["transfer"], "transfer_link": used}


def _is_better_failure(failure: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """区域级归因优先选取：有反事实致因的批次 > 到达更早的批次。"""

    if current is None:
        return True
    if bool(failure.get("causes")) != bool(current.get("causes")):
        return bool(failure.get("causes"))
    if failure.get("arrival") is None:
        return current.get("arrival") is None
    if current.get("arrival") is None:
        return True
    return failure["arrival"] < current["arrival"]


def _failure_for_batch(network: TransitNetwork, starts: dict[str, int],                       zones: dict[str, list[str]], zone_deadline: dict[str, int],
                       deadline: int, commute_limit: int, threshold: int) -> dict[str, Any] | None:
    """对一个出发批次做基线与三组反事实搜索并归因。"""

    start = min(starts.values())
    base = earliest_arrival(network, starts, capacity_threshold=threshold)
    base_pick = _pick(base, zones, zone_deadline, start, commute_limit, deadline)
    if base_pick and _feasible(base_pick["arrival"], start, base_pick["zone_end"], commute_limit):
        return None

    causes = []
    for mode, relax in RELAX_MODES:
        relaxed = earliest_arrival(network, starts, capacity_threshold=threshold, **relax)
        pick = _pick(relaxed, zones, zone_deadline, start, commute_limit, deadline)
        if pick is None or not _feasible(pick["arrival"], start, pick["zone_end"], commute_limit):
            continue
        base_arrival = base_pick["arrival"] if base_pick else None
        saved = (base_arrival - pick["arrival"]) if base_arrival is not None else None
        detail = _cause_detail(network, mode, base, relaxed, pick["node_id"], threshold)
        causes.append({"constraint": mode, "saved_minutes": saved,
                       "counterfactual_zone_id": pick["zone_id"],
                       "counterfactual_node_id": pick["node_id"],
                       "counterfactual_arrival": pick["arrival"],
                       "counterfactual_arrival_hhmm": hhmm(pick["arrival"]), **detail})
    causes.sort(key=lambda c: (c["counterfactual_arrival"] // CAUSE_TIE_BUCKET,
                               CAUSE_PREFERENCE[c["constraint"]],
                               c["counterfactual_arrival"],
                               -(c["saved_minutes"] or 0)))

    nearest = base_pick
    zone_end = nearest["zone_end"] if nearest else deadline
    arrival_value = nearest["arrival"] if nearest else None
    over_deadline = (arrival_value is not None and arrival_value > zone_end)
    over_limit = (arrival_value is not None and arrival_value - start > commute_limit)
    if causes:
        primary = causes[0]["constraint"]
        structural = False
    elif nearest is None:
        primary = "unreachable"
        structural = True
    else:
        primary = "deadline" if over_deadline else "commute_limit"
        structural = False
    return {
        "zone_id": nearest["zone_id"] if nearest else None,
        "nearest_node": nearest["node_id"] if nearest else None,
        "arrival": arrival_value,
        "arrival_hhmm": hhmm(arrival_value),
        "deadline": zone_end,
        "deadline_hhmm": hhmm(zone_end),
        "commute_limit": commute_limit,
        "late_minutes": (arrival_value - zone_end) if over_deadline else None,
        "over_limit_minutes": (arrival_value - start - commute_limit) if over_limit else None,
        "causes": causes,
        "primary_cause": primary,
        "structural": structural,
    }


def normalize_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化评估参数。"""

    params = params or {}
    deadline = to_minutes(params.get("deadline", "09:00"), "deadline")
    commute_limit = params.get("commute_limit", 60)
    _require(isinstance(commute_limit, int) and not isinstance(commute_limit, bool) and commute_limit > 0,
             "commute_limit 必须是正整数")
    threshold = params.get("capacity_threshold", 1)
    _require(isinstance(threshold, int) and not isinstance(threshold, bool) and threshold >= 1,
             "capacity_threshold 必须是 >=1 的整数")
    batches_raw = params.get("batches", ["07:00", "07:15", "07:30"])
    batches = sorted({to_minutes(b, f"batches[{i}]")
                      for i, b in enumerate(_sequence(batches_raw, "batches"))})
    _require(batches, "batches 至少需要一个出发时刻")
    return {"deadline": deadline, "commute_limit": commute_limit,
            "capacity_threshold": threshold, "batches": batches}


def evaluate(snapshot: dict[str, dict[str, Any]], raw_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """评估冻结快照，返回可冻结的覆盖结果（含路径、余量与归因）。"""

    network = build_network(snapshot)
    zone_doc = _mapping(snapshot["zone"], "zone")
    params = normalize_params(raw_params or {})
    deadline = params["deadline"]
    commute_limit = params["commute_limit"]
    threshold = params["capacity_threshold"]
    batches = params["batches"]

    zones: dict[str, list[str]] = {}
    zone_names: dict[str, str] = {}
    zone_deadline: dict[str, int] = {}
    for item in _sequence(zone_doc.get("employment_zones", []), "zone.employment_zones"):
        item = _mapping(item, "zone.employment_zones[]")
        zone_id = str(item.get("zone_id", "")).strip()
        _require(bool(zone_id) and zone_id not in zones, f"就业片区 {zone_id} 无效或重复")
        node_ids = [str(n).strip() for n in _sequence(item.get("node_ids"), f"zone {zone_id}.node_ids")]
        _require(bool(node_ids), f"就业片区 {zone_id} 至少包含一个节点")
        for node_id in node_ids:
            _require(node_id in network.nodes, f"就业片区 {zone_id} 引用未知节点 {node_id}")
        zones[zone_id] = node_ids
        zone_names[zone_id] = str(item.get("name", zone_id))
        zone_deadline[zone_id] = (to_minutes(item["deadline"], f"zone {zone_id}.deadline")
                                  if item.get("deadline") is not None else deadline)
    _require(zones, "zone.employment_zones 至少需要一个就业片区")

    areas_out = []
    covered_count = 0
    batch_covered: dict[int, int] = {b: 0 for b in batches}
    residential = _sequence(zone_doc.get("residential_areas", []), "zone.residential_areas")
    _require(residential, "zone.residential_areas 至少需要一个居住片区")
    for item in residential:
        item = _mapping(item, "zone.residential_areas[]")
        area_id = str(item.get("area_id", "")).strip()
        _require(bool(area_id), "area_id 不能为空")
        _require(not any(a["area_id"] == area_id for a in areas_out), f"居住片区 {area_id} 重复")
        origin_nodes = [str(n).strip() for n in _sequence(item.get("node_ids"), f"area {area_id}.node_ids")]
        _require(bool(origin_nodes), f"居住片区 {area_id} 至少包含一个节点")
        for node_id in origin_nodes:
            _require(node_id in network.nodes, f"居住片区 {area_id} 引用未知节点 {node_id}")

        area_covered = False
        reachable_zone_ids: set[str] = set()
        batch_entries = []
        area_failure: dict[str, Any] | None = None
        for batch in batches:
            starts = {node: batch for node in origin_nodes}
            search = earliest_arrival(network, starts, capacity_threshold=threshold)
            pick = _pick(search, zones, zone_deadline, batch, commute_limit, deadline)
            covered = pick is not None and _feasible(
                pick["arrival"], batch, pick["zone_end"], commute_limit)
            if covered:
                area_covered = True
                batch_covered[batch] += 1
                reachable_zone_ids.add(pick["zone_id"])
                batch_entries.append({
                    "batch": batch, "batch_hhmm": hhmm(batch), "covered": True,
                    "zone_id": pick["zone_id"], "arrive_node": pick["node_id"],
                    "arrival": pick["arrival"], "arrival_hhmm": hhmm(pick["arrival"]),
                    "total_minutes": pick["arrival"] - batch,
                    "path": reconstruct_path(network, search, pick["node_id"]),
                    "failure": None,
                })
            else:
                failure = _failure_for_batch(network, starts, zones, zone_deadline,
                                             deadline, commute_limit, threshold)
                batch_entries.append({
                    "batch": batch, "batch_hhmm": hhmm(batch), "covered": False,
                    "zone_id": pick["zone_id"] if pick else None,
                    "arrive_node": pick["node_id"] if pick else None,
                    "arrival": pick["arrival"] if pick else None,
                    "arrival_hhmm": hhmm(pick["arrival"]) if pick else None,
                    "total_minutes": (pick["arrival"] - batch) if pick else None,
                    "path": None, "failure": failure,
                })
                if failure is not None and not area_covered and _is_better_failure(
                        failure, area_failure):
                    area_failure = {"batch": batch, "batch_hhmm": hhmm(batch), **failure}
        # 若后续批次恢复覆盖，区域整体视为覆盖，不保留单批次失败作为区域致因
        if area_covered:
            area_failure = None
        if area_covered:
            covered_count += 1
        areas_out.append({
            "area_id": area_id,
            "name": str(item.get("name", area_id)),
            "origin_node_ids": origin_nodes,
            "covered": area_covered,
            "reachable_zone_ids": sorted(reachable_zone_ids),
            "failure_summary": area_failure,
            "batches": batch_entries,
        })

    total = len(areas_out)
    zones_reached = sorted({z for area in areas_out if area["covered"]
                            for z in area["reachable_zone_ids"]})
    return {
        "schema": RESULT_SCHEMA,
        "params": {
            "deadline": deadline, "deadline_hhmm": hhmm(deadline),
            "commute_limit": commute_limit, "capacity_threshold": threshold,
            "batches": [{"batch": b, "batch_hhmm": hhmm(b)} for b in batches],
        },
        "employment_zones": [{"zone_id": z, "name": zone_names[z], "node_ids": zones[z],
                              "deadline": zone_deadline[z], "deadline_hhmm": hhmm(zone_deadline[z])}
                             for z in zones],
        "areas": areas_out,
        "summary": {
            "areas_total": total,
            "areas_covered": covered_count,
            "coverage_ratio": round(covered_count / total, 6) if total else 0.0,
            "zones_reached": zones_reached,
            "batch_coverage": [
                {"batch": b, "batch_hhmm": hhmm(b), "areas_covered": batch_covered[b],
                 "coverage_ratio": round(batch_covered[b] / total, 6) if total else 0.0}
                for b in batches
            ],
        },
    }


# ---------------------------------------------------------------------------
# 方案对比
# ---------------------------------------------------------------------------

def compare_plans(result_a: dict[str, Any], result_b: dict[str, Any]) -> dict[str, Any]:
    """对比两版冻结结果，指出新增/失去覆盖及其约束致因。"""

    a_map = {a["area_id"]: a for a in result_a.get("areas", [])}
    b_map = {a["area_id"]: a for a in result_b.get("areas", [])}
    gained, lost, stable, changed_path = [], [], [], []
    for area_id in sorted(set(a_map) | set(b_map)):
        a, b = a_map.get(area_id), b_map.get(area_id)
        if a is None:
            gained.append({"area_id": area_id, "relieved_constraints": None,
                           "note": "仅存在于新版方案的居住片区"})
            continue
        if b is None:
            lost.append({"area_id": area_id, "causes": None,
                         "note": "居住片区在新版方案中缺失"})
            continue
        if not a["covered"] and b["covered"]:
            relieved = (a.get("failure_summary") or {}).get("causes") or []
            gained.append({
                "area_id": area_id,
                "previous_primary_cause": (a.get("failure_summary") or {}).get("primary_cause"),
                "relieved_constraints": [c["constraint"] for c in relieved],
                "previous_failure": a.get("failure_summary"),
            })
        elif a["covered"] and not b["covered"]:
            failure = b.get("failure_summary") or {}
            lost.append({
                "area_id": area_id,
                "primary_cause": failure.get("primary_cause"),
                "causes": [c["constraint"] for c in failure.get("causes", [])],
                "failure": failure,
            })
        elif a["covered"] and b["covered"]:
            stable.append(area_id)
            a_best = min((x for x in a["batches"] if x["covered"]), key=lambda x: x["arrival"], default=None)
            b_best = min((x for x in b["batches"] if x["covered"]), key=lambda x: x["arrival"], default=None)
            if a_best and b_best:
                a_services = [l["service_id"] for l in (a_best.get("path") or []) if l["leg_type"] == "ride"]
                b_services = [l["service_id"] for l in (b_best.get("path") or []) if l["leg_type"] == "ride"]
                if (a_best["arrival"] != b_best["arrival"]
                        or a_best["zone_id"] != b_best["zone_id"] or a_services != b_services):
                    changed_path.append({
                        "area_id": area_id,
                        "old_arrival": a_best["arrival"], "old_arrival_hhmm": hhmm(a_best["arrival"]),
                        "new_arrival": b_best["arrival"], "new_arrival_hhmm": hhmm(b_best["arrival"]),
                        "arrival_delta_minutes": b_best["arrival"] - a_best["arrival"],
                        "old_zone_id": a_best["zone_id"], "new_zone_id": b_best["zone_id"],
                        "old_services": a_services, "new_services": b_services,
                    })

    zones_a = set(result_a.get("summary", {}).get("zones_reached", []))
    zones_b = set(result_b.get("summary", {}).get("zones_reached", []))
    batch_a = {x["batch"]: x["coverage_ratio"] for x in result_a.get("summary", {}).get("batch_coverage", [])}
    batch_b = {x["batch"]: x["coverage_ratio"] for x in result_b.get("summary", {}).get("batch_coverage", [])}
    ratio_a = result_a.get("summary", {}).get("coverage_ratio", 0.0)
    ratio_b = result_b.get("summary", {}).get("coverage_ratio", 0.0)
    return {
        "schema": "commute-comparison/1",
        "plan_a": result_a.get("plan_id"),
        "plan_b": result_b.get("plan_id"),
        "coverage_ratio_a": ratio_a,
        "coverage_ratio_b": ratio_b,
        "coverage_ratio_delta": round(ratio_b - ratio_a, 6),
        "areas_gained": gained,
        "areas_lost": lost,
        "areas_stable_covered": stable,
        "paths_changed": changed_path,
        "zones_gained": sorted(zones_b - zones_a),
        "zones_lost": sorted(zones_a - zones_b),
        "batch_ratio_delta": [
            {"batch": b, "batch_hhmm": hhmm(b),
             "ratio_a": batch_a.get(b, 0.0), "ratio_b": batch_b.get(b, 0.0),
             "delta": round(batch_b.get(b, 0.0) - batch_a.get(b, 0.0), 6)}
            for b in sorted(set(batch_a) | set(batch_b))
        ],
    }
