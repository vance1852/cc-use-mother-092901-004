"""通勤覆盖评估引擎：解析场景快照、计算一小时可达范围并归因约束。

引擎为纯函数实现，不接触存储层。输入是场景快照（网络拓扑、班次、站内换乘、
首末班限制、容量门槛）与评估参数（出发批次、重点就业片区、通勤阈值等），输出
每个（出发批次, 就业片区）对的可达性结论：可达时给出冻结路径与拥挤余量，不可达
时给出等待、容量或换乘三类约束中起决定作用的一段。
"""

from __future__ import annotations

import heapq
import itertools
import re
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import ValidationError

MAX_MINUTE = 2880
DIAGNOSIS_HORIZON = 4320
MAX_CAPACITY_ATTEMPTS = 8
DEFAULT_COMMUTE_THRESHOLD_MINUTES = 60
DEFAULT_MAX_TRANSFER_MINUTES = 30
INF = float("inf")

CHANGE_TYPES = frozenset({"initial", "line_suspension", "extra_train", "timetable_revision"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _fail(message: str) -> None:
    raise ValidationError(message)


def _check_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail(f"{field} 格式无效")
    return value


def _check_minute(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_MINUTE:
        _fail(f"{field} 必须是 0 到 {MAX_MINUTE} 之间的整数分钟")
    return value


def _check_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} 不能为空")
    return value.strip()


def validate_snapshot(snapshot: Any) -> None:
    """校验场景快照的结构与引用完整性。"""

    if not isinstance(snapshot, dict):
        _fail("snapshot 必须是对象")
    stations = snapshot.get("stations")
    lines = snapshot.get("lines")
    segments = snapshot.get("segments")
    trips = snapshot.get("trips")
    transfers = snapshot.get("transfers", [])
    windows = snapshot.get("service_windows", [])
    if not isinstance(stations, list) or not stations:
        _fail("snapshot.stations 必须是非空数组")
    if not isinstance(lines, list) or not lines:
        _fail("snapshot.lines 必须是非空数组")
    if not isinstance(segments, list):
        _fail("snapshot.segments 必须是数组")
    if not isinstance(trips, list):
        _fail("snapshot.trips 必须是数组")
    if not isinstance(transfers, list):
        _fail("snapshot.transfers 必须是数组")
    if not isinstance(windows, list):
        _fail("snapshot.service_windows 必须是数组")

    station_ids: set[str] = set()
    for station in stations:
        if not isinstance(station, dict):
            _fail("车站条目必须是对象")
        station_id = _check_id(station.get("station_id"), "station_id")
        _check_text(station.get("name"), "station.name")
        _check_id(station.get("zone_id"), "station.zone_id")
        if station_id in station_ids:
            _fail(f"车站 {station_id} 重复")
        station_ids.add(station_id)

    line_ids: set[str] = set()
    for line in lines:
        if not isinstance(line, dict):
            _fail("线路条目必须是对象")
        line_id = _check_id(line.get("line_id"), "line_id")
        _check_text(line.get("name"), "line.name")
        capacity = line.get("capacity")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            _fail(f"线路 {line_id} 的 capacity 必须是正整数")
        if line_id in line_ids:
            _fail(f"线路 {line_id} 重复")
        line_ids.add(line_id)

    segment_keys: set[tuple[str, str, str]] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            _fail("区间条目必须是对象")
        line_id = _check_id(segment.get("line_id"), "segment.line_id")
        from_station = _check_id(segment.get("from_station"), "segment.from_station")
        to_station = _check_id(segment.get("to_station"), "segment.to_station")
        minutes = segment.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            _fail("segment.minutes 必须是正整数")
        if line_id not in line_ids:
            _fail(f"区间引用了不存在的线路 {line_id}")
        if from_station not in station_ids or to_station not in station_ids:
            _fail("区间引用了不存在的车站")
        if from_station == to_station:
            _fail("区间两端车站不能相同")
        key = (line_id, from_station, to_station)
        if key in segment_keys:
            _fail(f"区间 {line_id}:{from_station}->{to_station} 重复")
        segment_keys.add(key)

    trip_ids: set[str] = set()
    for trip in trips:
        if not isinstance(trip, dict):
            _fail("班次条目必须是对象")
        trip_id = _check_id(trip.get("trip_id"), "trip_id")
        line_id = _check_id(trip.get("line_id"), "trip.line_id")
        if line_id not in line_ids:
            _fail(f"班次 {trip_id} 引用了不存在的线路 {line_id}")
        stops = trip.get("stops")
        if not isinstance(stops, list) or len(stops) < 2:
            _fail(f"班次 {trip_id} 至少需要两个经停站")
        if trip_id in trip_ids:
            _fail(f"班次 {trip_id} 重复")
        trip_ids.add(trip_id)
        previous_depart = -1
        previous_station: str | None = None
        for stop in stops:
            if not isinstance(stop, (list, tuple)) or len(stop) != 3:
                _fail(f"班次 {trip_id} 的经停格式必须是 [车站, 到达分钟, 出发分钟]")
            stop_station = _check_id(stop[0], "trip.stop.station")
            if stop_station not in station_ids:
                _fail(f"班次 {trip_id} 经停不存在的车站 {stop_station}")
            arrive = _check_minute(stop[1], "trip.stop.arrive")
            depart = _check_minute(stop[2], "trip.stop.depart")
            if arrive > depart:
                _fail(f"班次 {trip_id} 在 {stop_station} 的到达时间晚于出发时间")
            if depart < previous_depart:
                _fail(f"班次 {trip_id} 的经停时间必须递增")
            if previous_station is not None and (line_id, previous_station, stop_station) not in segment_keys:
                _fail(f"班次 {trip_id} 经过的 {previous_station}->{stop_station} 缺少线路区间")
            previous_depart = depart
            previous_station = stop_station

    transfer_keys: set[tuple[str, str, str]] = set()
    for transfer in transfers:
        if not isinstance(transfer, dict):
            _fail("换乘条目必须是对象")
        station_id = _check_id(transfer.get("station_id"), "transfer.station_id")
        from_line = _check_id(transfer.get("from_line"), "transfer.from_line")
        to_line = _check_id(transfer.get("to_line"), "transfer.to_line")
        walk = transfer.get("walk_minutes")
        if station_id not in station_ids:
            _fail(f"换乘引用了不存在的车站 {station_id}")
        if from_line not in line_ids or to_line not in line_ids:
            _fail("换乘引用了不存在的线路")
        if from_line == to_line:
            _fail("换乘的起止线路不能相同")
        if not isinstance(walk, int) or isinstance(walk, bool) or not 0 <= walk <= 120:
            _fail("transfer.walk_minutes 必须是 0 到 120 的整数")
        key = (station_id, from_line, to_line)
        if key in transfer_keys:
            _fail(f"换乘 {station_id}:{from_line}->{to_line} 重复")
        transfer_keys.add(key)

    window_lines: set[str] = set()
    for window in windows:
        if not isinstance(window, dict):
            _fail("首末班条目必须是对象")
        line_id = _check_id(window.get("line_id"), "service_window.line_id")
        if line_id not in line_ids:
            _fail(f"首末班限制引用了不存在的线路 {line_id}")
        first = _check_minute(window.get("first_minute"), "service_window.first_minute")
        last = _check_minute(window.get("last_minute"), "service_window.last_minute")
        if first >= last:
            _fail(f"线路 {line_id} 的首班必须早于末班")
        if line_id in window_lines:
            _fail(f"线路 {line_id} 的首末班限制重复")
        window_lines.add(line_id)

    threshold = snapshot.get("capacity_threshold")
    if threshold is not None:
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
            _fail("snapshot.capacity_threshold 必须在 (0, 1] 之间")


@dataclass(frozen=True)
class Trip:
    """一个班次及其经停序列（车站, 到达分钟, 出发分钟）。"""

    trip_id: str
    line_id: str
    stops: tuple[tuple[str, int, int], ...]


class Network:
    """把校验过的快照整理成引擎可用的索引结构。"""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        validate_snapshot(snapshot)
        self.capacity = {line["line_id"]: line["capacity"] for line in snapshot["lines"]}
        self.zone_stations: dict[str, list[str]] = {}
        for station in snapshot["stations"]:
            self.zone_stations.setdefault(station["zone_id"], []).append(station["station_id"])
        for stations in self.zone_stations.values():
            stations.sort()
        self.windows = {
            window["line_id"]: (window["first_minute"], window["last_minute"])
            for window in snapshot.get("service_windows", [])
        }
        self.transfer_walk = {
            (item["station_id"], item["from_line"], item["to_line"]): item["walk_minutes"]
            for item in snapshot.get("transfers", [])
        }
        self.trip_by_id: dict[str, Trip] = {}
        self.trips_at: dict[str, list[tuple[Trip, int]]] = {}
        for raw in snapshot.get("trips", []):
            trip = Trip(raw["trip_id"], raw["line_id"],
                        tuple((stop[0], stop[1], stop[2]) for stop in raw["stops"]))
            self.trip_by_id[trip.trip_id] = trip
            for index, (station, _arrive, _depart) in enumerate(trip.stops[:-1]):
                self.trips_at.setdefault(station, []).append((trip, index))
        for entries in self.trips_at.values():
            entries.sort(key=lambda item: (item[0].stops[item[1]][2], item[0].trip_id))


def normalize_parameters(snapshot: dict[str, Any], parameters: Any) -> dict[str, Any]:
    """校验并补全评估参数，返回可冻结的规范化版本。"""

    if not isinstance(parameters, dict):
        _fail("parameters 必须是对象")
    known_zones = {station["zone_id"] for station in snapshot["stations"]}

    threshold = parameters.get("commute_threshold_minutes", DEFAULT_COMMUTE_THRESHOLD_MINUTES)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 1 <= threshold <= 720:
        _fail("commute_threshold_minutes 必须是 1 到 720 的整数")

    capacity_threshold = parameters.get("capacity_threshold", snapshot.get("capacity_threshold", 1.0))
    if not isinstance(capacity_threshold, (int, float)) or isinstance(capacity_threshold, bool) \
            or not 0 < capacity_threshold <= 1:
        _fail("capacity_threshold 必须在 (0, 1] 之间")
    capacity_threshold = float(capacity_threshold)

    max_transfer = parameters.get("max_transfer_minutes", DEFAULT_MAX_TRANSFER_MINUTES)
    if not isinstance(max_transfer, int) or isinstance(max_transfer, bool) or not 0 <= max_transfer <= 240:
        _fail("max_transfer_minutes 必须是 0 到 240 的整数")

    zones = parameters.get("employment_zones")
    if not isinstance(zones, list) or not zones:
        _fail("employment_zones 必须是非空数组")
    norm_zones: list[str] = []
    for zone in zones:
        zone_id = _check_id(zone, "employment_zone")
        if zone_id not in known_zones:
            _fail(f"就业片区 {zone_id} 在网络中不存在")
        if zone_id in norm_zones:
            _fail(f"就业片区 {zone_id} 重复")
        norm_zones.append(zone_id)

    batches = parameters.get("departure_batches")
    if not isinstance(batches, list) or not batches:
        _fail("departure_batches 必须是非空数组")
    norm_batches: list[dict[str, Any]] = []
    seen_batches: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            _fail("出发批次必须是对象")
        batch_id = _check_id(batch.get("batch_id"), "batch_id")
        if batch_id in seen_batches:
            _fail(f"出发批次 {batch_id} 重复")
        seen_batches.add(batch_id)
        origin_zone = _check_id(batch.get("origin_zone"), "origin_zone")
        if origin_zone not in known_zones:
            _fail(f"出发批次的来源片区 {origin_zone} 不存在")
        depart_minute = _check_minute(batch.get("depart_minute"), "depart_minute")
        demand = batch.get("demand")
        if not isinstance(demand, int) or isinstance(demand, bool) or demand < 1:
            _fail(f"批次 {batch_id} 的 demand 必须是正整数")
        demands = batch.get("demands")
        norm_demands: dict[str, int] | None = None
        if demands is not None:
            if not isinstance(demands, dict) or not demands:
                _fail(f"批次 {batch_id} 的 demands 必须是非空对象")
            norm_demands = {}
            for zone, value in demands.items():
                zone_id = _check_id(zone, "demands.zone")
                if zone_id not in known_zones:
                    _fail(f"批次 {batch_id} 的 demands 引用了未知片区 {zone_id}")
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    _fail(f"批次 {batch_id} 的 demands 取值必须是正整数")
                norm_demands[zone_id] = value
        norm_batches.append({"batch_id": batch_id, "origin_zone": origin_zone,
                             "depart_minute": depart_minute, "demand": demand,
                             "demands": norm_demands})

    return {
        "commute_threshold_minutes": threshold,
        "capacity_threshold": capacity_threshold,
        "max_transfer_minutes": max_transfer,
        "employment_zones": norm_zones,
        "departure_batches": norm_batches,
    }


def _search(network: Network, origins: list[str], depart_minute: int, horizon: int,
            blocked: frozenset[tuple[str, int]] | set[tuple[str, int]] | tuple,
            relax_transfers: bool, max_transfer: int) -> tuple[dict, dict]:
    """在 (车站, 到达线路) 状态空间上做最早到达搜索。

    blocked 中的 (班次, 区间序号) 不可通行；relax_transfers 为真时忽略换乘规则
    缺失与换乘步行上限（用于归因诊断的下界估计）。
    """

    dist: dict[tuple[str, str | None], int] = {}
    prev: dict[tuple[str, str | None], Any] = {}
    heap: list[tuple[int, int, tuple[str, str | None]]] = []
    counter = itertools.count()
    for station in origins:
        state = (station, None)
        if depart_minute < dist.get(state, INF):
            dist[state] = depart_minute
            prev[state] = None
            heapq.heappush(heap, (depart_minute, next(counter), state))
    while heap:
        moment, _, state = heapq.heappop(heap)
        if moment > dist.get(state, INF):
            continue
        station, arrived_line = state
        for trip, index in network.trips_at.get(station, ()):
            line = trip.line_id
            depart = trip.stops[index][2]
            window = network.windows.get(line)
            if window is not None and not window[0] <= depart <= window[1]:
                continue
            transfer = None
            if arrived_line is None or arrived_line == line:
                ready = moment
            else:
                walk = network.transfer_walk.get((station, arrived_line, line))
                assumed = walk is None
                if assumed:
                    if not relax_transfers:
                        continue
                    walk = 0
                elif walk > max_transfer and not relax_transfers:
                    continue
                ready = moment + walk
                transfer = {"station": station, "from_line": arrived_line, "to_line": line,
                            "walk_minutes": walk, "assumed": assumed}
            if depart < ready:
                continue
            wait = depart - ready
            for target_index in range(index + 1, len(trip.stops)):
                if (trip.trip_id, target_index - 1) in blocked:
                    break
                next_station = trip.stops[target_index][0]
                arrive = trip.stops[target_index][1]
                if arrive > horizon:
                    continue
                next_state = (next_station, line)
                if arrive < dist.get(next_state, INF):
                    dist[next_state] = arrive
                    prev[next_state] = (state, {
                        "trip_id": trip.trip_id, "line_id": line,
                        "board_station": station, "board_index": index,
                        "alight_station": next_station, "alight_index": target_index,
                        "depart_minute": depart, "arrive_minute": arrive,
                        "wait_minutes": wait, "transfer": transfer,
                    })
                    heapq.heappush(heap, (arrive, next(counter), next_state))
    return dist, prev


def _best_target(dist: dict, targets: list[str]) -> tuple[str, str | None] | None:
    target_set = set(targets)
    best: tuple[tuple[int, str, str], tuple[str, str | None]] | None = None
    for state, moment in dist.items():
        if state[0] in target_set:
            key = (moment, state[0], state[1] or "")
            if best is None or key < best[0]:
                best = (key, state)
    return None if best is None else best[1]


def _reconstruct(prev: dict, state: tuple[str, str | None]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    cursor = state
    while True:
        entry = prev[cursor]
        if entry is None:
            break
        cursor, leg = entry
        legs.append(leg)
    legs.reverse()
    return legs


def _public_path(legs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "legs": [{"trip_id": leg["trip_id"], "line_id": leg["line_id"],
                  "board_station": leg["board_station"], "alight_station": leg["alight_station"],
                  "depart_minute": leg["depart_minute"], "arrive_minute": leg["arrive_minute"]}
                 for leg in legs],
        "transfers": [{"station": leg["transfer"]["station"],
                       "from_line": leg["transfer"]["from_line"],
                       "to_line": leg["transfer"]["to_line"],
                       "walk_minutes": leg["transfer"]["walk_minutes"]}
                      for leg in legs if leg["transfer"]],
    }


def _path_edges(legs: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], int]]:
    for leg in legs:
        for segment_index in range(leg["board_index"], leg["alight_index"]):
            yield leg, segment_index


def _capacity_deficits(network: Network, legs: list[dict[str, Any]], loads: dict,
                       flow: int, capacity_threshold: float) -> list[dict[str, Any]]:
    deficits = []
    for leg, segment_index in _path_edges(legs):
        capacity = network.capacity[leg["line_id"]]
        remaining = capacity * capacity_threshold - loads.get((leg["trip_id"], segment_index), 0)
        if remaining < flow:
            trip = network.trip_by_id[leg["trip_id"]]
            deficits.append({
                "edge": (leg["trip_id"], segment_index),
                "trip_id": leg["trip_id"], "line_id": leg["line_id"],
                "from_station": trip.stops[segment_index][0],
                "to_station": trip.stops[segment_index + 1][0],
                "required": flow, "remaining": round(max(0.0, remaining), 4),
            })
    return deficits


def _assign_loads(legs: list[dict[str, Any]], loads: dict, flow: int) -> None:
    for leg, segment_index in _path_edges(legs):
        key = (leg["trip_id"], segment_index)
        loads[key] = loads.get(key, 0) + flow


def _crowding_margin(network: Network, legs: list[dict[str, Any]], loads: dict) -> float | None:
    margin: float | None = None
    for leg, segment_index in _path_edges(legs):
        capacity = network.capacity[leg["line_id"]]
        headroom = (capacity - loads.get((leg["trip_id"], segment_index), 0)) / capacity
        margin = headroom if margin is None else min(margin, headroom)
    return round(margin, 4) if margin is not None else None


def _evaluate_pair(network: Network, batch: dict[str, Any], zone: str, flow: int,
                   loads: dict, params: dict[str, Any]) -> dict[str, Any]:
    depart_minute = batch["depart_minute"]
    horizon = depart_minute + params["commute_threshold_minutes"]
    origins = network.zone_stations[batch["origin_zone"]]
    targets = network.zone_stations[zone]
    blocked: set[tuple[str, int]] = set()
    failures: list[list[dict[str, Any]]] = []
    for _attempt in range(MAX_CAPACITY_ATTEMPTS):
        dist, prev = _search(network, origins, depart_minute, horizon, blocked,
                             relax_transfers=False, max_transfer=params["max_transfer_minutes"])
        state = _best_target(dist, targets)
        if state is None:
            break
        legs = _reconstruct(prev, state)
        deficits = _capacity_deficits(network, legs, loads, flow, params["capacity_threshold"])
        if not deficits:
            _assign_loads(legs, loads, flow)
            arrive = dist[state]
            return {
                "batch_id": batch["batch_id"], "zone_id": zone, "status": "covered",
                "depart_minute": depart_minute, "arrive_minute": arrive,
                "duration_minutes": arrive - depart_minute,
                "total_wait_minutes": sum(leg["wait_minutes"] for leg in legs),
                "crowding_margin": _crowding_margin(network, legs, loads),
                "path": _public_path(legs), "reason": None,
            }
        failures.append(deficits)
        for deficit in deficits:
            blocked.add(deficit["edge"])
    reason = _diagnose(network, batch, zone, flow, params, failures, origins, targets, depart_minute)
    return {"batch_id": batch["batch_id"], "zone_id": zone, "status": "uncovered",
            "depart_minute": depart_minute, "reason": reason}


def _diagnose(network: Network, batch: dict[str, Any], zone: str, flow: int,
              params: dict[str, Any], failures: list[list[dict[str, Any]]],
              origins: list[str], targets: list[str], depart_minute: int) -> dict[str, Any]:
    """按容量、换乘、等待的顺序定位起决定作用的约束。"""

    threshold = params["commute_threshold_minutes"]
    horizon = depart_minute + threshold
    if failures:
        worst = min((deficit for failure in failures for deficit in failure),
                    key=lambda item: (item["remaining"], item["trip_id"]))
        return {"type": "capacity", "trip_id": worst["trip_id"], "line_id": worst["line_id"],
                "from_station": worst["from_station"], "to_station": worst["to_station"],
                "required": worst["required"], "remaining": worst["remaining"],
                "message": f"班次 {worst['trip_id']} 区间 {worst['from_station']}->{worst['to_station']} "
                           f"剩余容量 {worst['remaining']} 低于批次需求 {worst['required']}"}
    dist, prev = _search(network, origins, depart_minute, DIAGNOSIS_HORIZON, frozenset(),
                         relax_transfers=True, max_transfer=params["max_transfer_minutes"])
    state = _best_target(dist, targets)
    if state is not None and dist[state] <= horizon:
        legs = _reconstruct(prev, state)
        for leg in legs:
            transfer = leg["transfer"]
            if transfer and transfer["assumed"]:
                return {"type": "transfer", "station": transfer["station"],
                        "from_line": transfer["from_line"], "to_line": transfer["to_line"],
                        "issue": "no_transfer_rule",
                        "message": f"车站 {transfer['station']} 缺少 "
                                   f"{transfer['from_line']}->{transfer['to_line']} 的站内换乘通道"}
            if transfer and transfer["walk_minutes"] > params["max_transfer_minutes"]:
                return {"type": "transfer", "station": transfer["station"],
                        "from_line": transfer["from_line"], "to_line": transfer["to_line"],
                        "issue": "walk_exceeds_limit", "walk_minutes": transfer["walk_minutes"],
                        "message": f"车站 {transfer['station']} 换乘步行 {transfer['walk_minutes']} 分钟 "
                                   f"超过上限 {params['max_transfer_minutes']} 分钟"}
        return {"type": "transfer", "station": None, "from_line": None, "to_line": None,
                "issue": "transfer_constraint",
                "message": "仅在放宽换乘约束后才可达，归因于站内换乘限制"}
    if state is None:
        return {"type": "waiting", "issue": "no_reachable_trip",
                "message": "在首末班与时刻约束下没有任何可衔接的班次"}
    legs = _reconstruct(prev, state)
    longest = max(legs, key=lambda leg: leg["wait_minutes"])
    overshoot = dist[state] - horizon
    return {"type": "waiting", "issue": "arrival_beyond_threshold",
            "earliest_arrive_minute": dist[state], "overshoot_minutes": overshoot,
            "longest_wait": {"station": longest["board_station"], "line_id": longest["line_id"],
                             "wait_minutes": longest["wait_minutes"]},
            "message": f"最早 {dist[state]} 分钟到达，超出通勤阈值 {overshoot} 分钟，"
                       f"在 {longest['board_station']} 等候 {longest['line_id']} "
                       f"{longest['wait_minutes']} 分钟"}


def evaluate_commute(snapshot: dict[str, Any], parameters: Any) -> dict[str, Any]:
    """评估全部（出发批次, 就业片区）对，返回可冻结的完整结果。"""

    network = Network(snapshot)
    params = normalize_parameters(snapshot, parameters)
    loads: dict[tuple[str, int], int] = {}
    outcomes: list[dict[str, Any]] = []
    flows: list[tuple[dict[str, Any], int]] = []
    batches = sorted(params["departure_batches"],
                     key=lambda item: (item["depart_minute"], item["batch_id"]))
    for batch in batches:
        for zone in params["employment_zones"]:
            if zone == batch["origin_zone"]:
                continue
            demands = batch["demands"] or {}
            flow = demands.get(zone, batch["demand"])
            outcome = _evaluate_pair(network, batch, zone, flow, loads, params)
            outcomes.append(outcome)
            flows.append((outcome, flow))
    covered = sum(1 for outcome, _flow in flows if outcome["status"] == "covered")
    total = len(flows)
    demand_total = sum(flow for _outcome, flow in flows)
    demand_covered = sum(flow for outcome, flow in flows if outcome["status"] == "covered")
    coverage = {
        "pairs": total,
        "covered": covered,
        "ratio": round(covered / total, 4) if total else None,
        "demand_total": demand_total,
        "demand_covered": demand_covered,
        "demand_ratio": round(demand_covered / demand_total, 4) if demand_total else None,
    }
    return {"parameters": params, "outcomes": outcomes, "coverage": coverage}


def _brief(outcome: dict[str, Any]) -> dict[str, Any]:
    return {"arrive_minute": outcome.get("arrive_minute"),
            "duration_minutes": outcome.get("duration_minutes"),
            "crowding_margin": outcome.get("crowding_margin")}


def compare_results(base_result: dict[str, Any], candidate_result: dict[str, Any]) -> dict[str, Any]:
    """对比两版冻结结果，逐对给出覆盖变化及其约束归因。"""

    base_map = {(item["batch_id"], item["zone_id"]): item for item in base_result["outcomes"]}
    candidate_map = {(item["batch_id"], item["zone_id"]): item for item in candidate_result["outcomes"]}
    counts = {"gained": 0, "lost": 0, "kept_covered": 0, "kept_uncovered": 0,
              "missing_in_base": 0, "missing_in_candidate": 0}
    changes = []
    for key in sorted(set(base_map) | set(candidate_map)):
        base = base_map.get(key)
        candidate = candidate_map.get(key)
        entry: dict[str, Any] = {"batch_id": key[0], "zone_id": key[1]}
        if base is None:
            entry["change"] = "missing_in_base"
        elif candidate is None:
            entry["change"] = "missing_in_candidate"
        elif base["status"] == "covered" and candidate["status"] == "covered":
            entry["change"] = "kept_covered"
        elif base["status"] != "covered" and candidate["status"] != "covered":
            entry["change"] = "kept_uncovered"
            entry["cause"] = candidate["reason"]
            entry["base_cause"] = base["reason"]
        elif base["status"] != "covered":
            entry["change"] = "gained"
            entry["cause"] = base["reason"]
            entry["current"] = _brief(candidate)
        else:
            entry["change"] = "lost"
            entry["cause"] = candidate["reason"]
            entry["previous"] = _brief(base)
        counts[entry["change"]] += 1
        changes.append(entry)
    base_coverage = base_result["coverage"]
    candidate_coverage = candidate_result["coverage"]
    if base_coverage["ratio"] is None or candidate_coverage["ratio"] is None:
        coverage_delta = None
    else:
        coverage_delta = round(candidate_coverage["ratio"] - base_coverage["ratio"], 4)
    summary = {"base_coverage": base_coverage, "candidate_coverage": candidate_coverage,
               "coverage_delta": coverage_delta, "counts": counts}
    return {"summary": summary, "changes": changes}
