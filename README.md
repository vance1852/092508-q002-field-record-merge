# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置、观察记录候选合并四个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约和鉴定决定；
- `src/biosafety_ops/`：库区记录、有害生物监测、风险告警、处置工单和资源分配；
- `src/observation_registry/`：多方观察上报、可解释候选合并打分、馆员合并/拆回决定、新证据撤销重判与统一观察追溯；
- `fixtures/`：离线验收使用的实验协议、结构化观察记录与雨后兰科多方上报样例；
- `tests/`：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m observation_registry.acceptance --workspace .
```

验收会建立临时 SQLite 数据库，登记馆藏环境指标、保藏库房、转运路线和材料批次，完成实验观察导入、异常复核、生物安全告警与资源分配；观察记录子域会复现雨后疑似珍稀兰科的护林员、志愿者、学校小组三方上报，完成候选打分、合并/拆回、新证据撤销重判、隐私取交集脱敏和双向追溯，并输出 JSON 结果。命令不访问公网，也不需要额外数据库、队列或常驻服务。

## 候选合并如何工作

`src/observation_registry/` 的合并流程分为三步：

1. **可解释打分（`matching.py`，纯函数，当前算法 `observation-match/1`）**：对任意两条记录按固定权重计算四个因子——地点（0.35，对比坐标误差圆是否相交/包含）、时间（0.20，按可配置时间窗口）、分类意见（0.25，相同类群取双方置信度低值、双方都有意见但不一致则封顶为需复核、一方无意见为中性）、观察材料（0.20，材料类型集合的 Jaccard 重合度）。误差圆不相交或超出时间窗口构成硬性否决。输出每因子的数值、权重、中文理由、总置信度（`merge ≥ 0.70`，其余 `review`/`no_match`）与证据指纹；
2. **馆员决定（`service.py`）**：curator 对挂出的 merge/review 候选执行合并或拆回。相同决定内容凭 `Idempotency-Key` 回放原结果；拒绝的候选保留拒绝理由。机器判为 `no_match` 的记录对不落单，可通过 `/candidates/evaluate` 随时重算并说明为何不合并；
3. **新证据重判与追溯**：鉴定意见或观察材料只追加。统一记录中任一来源出现晚于合并决定的新证据时，curator 可撤销合并，旧成员关系（谁在何时合并、谁在何时拆回）全部留痕，指纹变化的旧合并/旧拆回候选自动重新挂出。统一记录视图保留全部来源（含已拆回）与决策时间线；原始记录永不删除。

**隐私不扩大披露**：统一记录的位置可见级别取各来源最严隐私级别；命中受保护物种（原始标记或鉴定意见带 `protected` 标签）时位置仅馆员可见。查询者只能看到自己贡献的来源坐标，其余来源逐条脱敏（贡献者、审计可打开记录追溯时间线，但坐标仍按条遮盖）。

## HTTP API

```bash
PYTHONPATH=src python3 -m collection_logistics.api --database collection.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database taxonomy.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database biosafety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m observation_registry.api --database observation.sqlite3 --host 127.0.0.1 --port 8083
```

四个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可以继续查询与复核。

观察记录合并服务（8083）的主要接口：

- `POST /observations`（贡献者，支持 `Idempotency-Key`）上报原始记录；`POST /observations/{id}/opinions` 追加鉴定意见；`POST /observations/{id}/evidence` 追加新材料证据；
- `POST /candidates/suggest`（馆员）重算并挂出 merge/review 候选；`POST /candidates/evaluate` 按需解释任意两条记录为何不合并；`GET /candidates`、`GET /candidates/{id}` 查询候选与馆员决定；
- `POST /candidates/{id}/decision`（馆员，幂等）执行 `merge` 或 `reject`；`POST /canonical/{id}/undo` 在有新证据时撤销合并；
- `GET /canonical/{id}` 查询统一记录的全部来源、逐条脱敏坐标与决策时间线；`GET /observations/{id}` 从原始记录反向追溯所属统一记录与全部候选判断；`GET /audit`（馆员/审计）查看决策事件流。
