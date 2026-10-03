# 评估都市圈一小时通勤协同服务

本项目在综合交通运输共享基础能力（组织/操作者登记、角色权限、请求幂等、SQLite
事务、哈希串联审计）之上，提供**一小时通勤覆盖决策系统**：接收网络拓扑、班次版本、
站内换乘、首末班限制、容量门槛和重点就业片区，把不同出发批次的可达路径与拥挤余量
冻结成可复核的评估方案，并支持多部门方案比较、唯一获批与重启后续审。

## 核心决策规则

- **输入只增不改**：网络、班次、换乘、首末班、容量、片区六类资料按
  `(doc_type, doc_id, version)` 追加；线路停运、临时加班车、时刻修订都只能产生
  新版本，旧版本永久保留。
- **场景冻结**：创建场景时把引用的六个版本整体快照冻结并计算输入哈希；冻结前会
  完整校验跨文档一致性（如班次停靠与拓扑边、运行时长必须相符）。
- **报告不可静默改写**：方案在提交时按冻结快照计算覆盖结果并哈希固化；之后登记的
  任何新版本都不会影响已发布报告，变更必须通过新场景与新方案体现。
  `GET /plans/{id}/verify` 会同时校验存储哈希与冻结快照重算哈希。
- **批次可达性**：按多个出发批次做基于时刻的最早到达搜索（时间依赖 Dijkstra），
  路径逐腿记录等待、换乘耗时、首末班窗口与每个区间的拥挤余量；区域任一批次可达
  即视为覆盖，同时保留每个失败批次的归因。
- **约束归因**：对未覆盖区域执行三组反事实搜索（忽略等待/窗口、忽略容量门槛、
  换乘归零），指出覆盖失去是由哪段等待、容量或换乘约束造成；放松后仍不可达标记
  为结构性不可达（如线路停运）。
- **比较与审批**：可经 API 对比任意两版方案（新增/失去覆盖、致因约束、路径与
  到达时刻变化、分批次覆盖差），比较关系持久化；每个场景至多一个 `approved`
  方案（数据库部分唯一索引保证），其他部门方案需由 admin 显式 `supersede` 接替。
- **重启续审**：场景、方案、评审记录、比较关系全部落 SQLite，进程重启后可继续
  未完成的评审。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、
  通勤可达性引擎（`transit.py`）、决策工作流（`planning.py`）、HTTP 路由和离线验收；
- `tests/`：基础规则、引擎归因、事务边界、评审状态机、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令在临时 SQLite 数据库中完成基础登记与完整通勤决策链：登记六类输入、冻结
新旧两个时刻场景、提交并审批方案、对比两版覆盖（旧报告保持 100% 覆盖、新时刻因
换乘错位降为 50%），并核对审计链；成功时输出一行 `status` 为 `ok` 的 JSON 并以
退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后
SQLite 中的业务状态、冻结报告和审计历史继续保留。

### 决策接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 登记一份版本化输入（`doc_type` 为 `network`/`timetable`/`transfer`/`service_window`/`capacity`/`zone`） |
| GET | `/documents/{doc_type}/{doc_id}` | 查看某输入的全部历史版本 |
| POST | `/scenarios` | 用六类版本引用与评估参数冻结场景 |
| GET | `/scenarios`、`/scenarios/{id}` | 场景列表/详情 |
| POST | `/plans` | 在冻结场景上计算并固化覆盖结果（待评审） |
| GET | `/plans`、`/plans/{id}` | 方案列表（可按场景/状态/部门过滤）/详情（含路径、余量、归因） |
| GET | `/plans/{id}/verify` | 重算并核对冻结报告未被改写 |
| GET | `/plans/{id}/reviews` | 评审历史 |
| POST | `/reviews` | `approve`/`reject`/`comment`/`supersede` |
| POST | `/comparisons` | 对比任意两版方案，关系按方案对持久化 |
| GET | `/comparisons/{id}` | 读取比较结果 |

评估参数（`params`）：`deadline`（上班截止，默认 09:00）、`commute_limit`
（门到门分钟数，默认 60）、`capacity_threshold`（区间最低拥挤余量，默认 1）、
`batches`（出发时刻列表，默认 07:00/07:15/07:30）。

所有写接口沿用基础服务的 `request_id` 幂等：同一编号重放返回原始回执，编号绑定
不同内容返回 409。
