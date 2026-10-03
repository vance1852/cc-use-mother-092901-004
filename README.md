# 评估都市圈一小时通勤协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。在这些稳定边界之上，项目内置了**都市圈一小时通勤覆盖决策系统**：把网络拓扑、班次版本、站内换乘、首末班限制、容量门槛和重点就业片区冻结成可复核的评估方案，并支持多部门并行提交、两两对比与单一批准。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `commute.py`：通勤覆盖评估引擎（纯函数），负责可达路径搜索、拥挤余量计算与约束归因；
  - `commute_service.py`：场景、方案、评审的领域服务与工作流；
- `tests/`：基础规则、事务边界、接口路由、引擎归因和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 领域模型

### 场景（scenario）：不可变的运行数据快照

场景把某一版运行数据完整冻结：车站与片区、线路与容量、区间、班次（经停时刻）、站内换乘规则、首末班限制和容量门槛默认值。`change_type` 取值为：

- `initial`：首次建档；
- `line_suspension`：线路停运；
- `extra_train`：临时加班车；
- `timetable_revision`：时刻修订。

后三类必须引用同一场所下的 `base_scenario_id` 并提供非空 `change_detail`。场景一经创建不可修改——任何运行数据变化只会生成新场景，**已发布（获批）方案的评估结果不会被后来的运行数据静默改写**。

### 方案（plan）：创建即冻结的评估结果

创建方案时指定场景、评审事项（`case_id`）和评估参数（出发批次、重点就业片区、通勤阈值、容量门槛、换乘步行上限），服务立即完成评估并把结果随方案冻结：

- 每个（出发批次, 就业片区）对的可达路径（乘降区间、换乘、总等候）与拥挤余量；
- 不可达时的归因：`waiting`（等待，含最早到达、超出阈值分钟、最长等候段）、`capacity`（容量，含班次区间、剩余与需求）、`transfer`（换乘，含车站、起止线路、缺失通道或步行超限）；
- 覆盖比例（按对数与按需求人数两种口径）。

方案状态机：`draft → submitted → approved / rejected`。方案记录提交部门（`organization_id`），不同部门可对同一评审事项并行提交方案，比较关系通过评审保留。

### 评审（review）：可重启的对比与单一批准

评审把任意两版已提交方案的对比结果（逐对覆盖变化及归因、覆盖率差值）冻结持久化。审批人员裁决后评审完成；批准动作在事务内检查同一评审事项不存在其他获批方案，并由部分唯一索引兜底——**同一事项只允许一个版本获批**，并发批准只有一个成功。评审保存在 SQLite 中，进程重启后可通过 `GET /commute/reviews?status=open` 找回未完成的评审继续裁决。

## 快照与参数格式

```json
{
  "stations": [{"station_id": "R1", "name": "居住一", "zone_id": "ZR"}],
  "lines": [{"line_id": "L1", "name": "市域1号", "capacity": 200}],
  "segments": [{"line_id": "L1", "from_station": "R1", "to_station": "J1", "minutes": 20}],
  "trips": [{"trip_id": "T1", "line_id": "L1", "stops": [["R1", 415, 420], ["J1", 440, 440]]}],
  "transfers": [{"station_id": "M1", "from_line": "L1", "to_line": "L2", "walk_minutes": 4}],
  "service_windows": [{"line_id": "L1", "first_minute": 360, "last_minute": 1320}],
  "capacity_threshold": 0.9
}
```

时刻为午夜起算的整数分钟（0–2880）。评估参数：

```json
{
  "commute_threshold_minutes": 60,
  "capacity_threshold": 0.8,
  "max_transfer_minutes": 30,
  "employment_zones": ["ZJ"],
  "departure_batches": [{"batch_id": "B1", "origin_zone": "ZR", "depart_minute": 416, "demand": 80}]
}
```

## HTTP 接口

写入接口通过 `X-Actor-Id` 标识操作者，所有写操作按 `request_id` 幂等。

| 方法与路径 | 说明 | 角色 |
| --- | --- | --- |
| `POST /commute/scenarios` | 登记场景快照（停运/加班车/修订只生成新版本） | admin、operator |
| `GET /commute/scenarios?site_id=` / `GET /commute/scenarios/{id}` | 场景列表 / 含快照的详情 | 任意 |
| `POST /commute/plans` | 创建方案并冻结评估结果 | admin、operator |
| `GET /commute/plans?site_id=&case_id=&status=` / `GET /commute/plans/{id}` | 方案列表 / 含冻结结果的详情 | 任意 |
| `POST /commute/plans/{id}/submit` | 提交本部门方案进入评审 | admin、operator |
| `GET /commute/compare?base_plan_id=&candidate_plan_id=` | 任意两版方案的即时对比（含逐对归因） | 任意 |
| `POST /commute/reviews` | 创建评审并冻结对比结果 | admin、reviewer |
| `GET /commute/reviews?site_id=&status=` / `GET /commute/reviews/{id}` | 评审列表（重启后续办）/ 含对比的详情 | 任意 |
| `POST /commute/reviews/{id}/decision` | 裁决（`approve`/`reject`），批准受单一获批约束 | admin、reviewer |

基础接口（组织、操作者、场所、领域资料、审计事件）保持不变。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令在临时 SQLite 数据库中完成基础登记链，随后登记两版运行图场景（含临时加班车）、评估并提交两版方案、创建评审，**模拟进程重启后继续未完成的评审并批准**，核对幂等回执与审计链。成功时输出一行 `status` 为 `ok` 的 JSON（`commute.gained` 为 `1`、`open_reviews_after_restart` 为 `1`）并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。服务重启后 SQLite 中的业务状态、冻结方案、待办评审和审计历史继续保留。
