# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置、野外观察上报去重合并四个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约和鉴定决定；
- `src/biosafety_ops/`：库区记录、有害生物监测、风险告警、处置工单和资源分配；
- `src/field_observations/`：野外观察上报、后续鉴定意见、可解释的重复候选评估、馆员合并与拆回决策、统一观察记录溯源；
- `fixtures/`：离线验收使用的实验协议与结构化观察记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 野外观察去重合并

`field_observations` 把护林员、志愿者和学校小组对同一疑似物种的重复上报转为可解释的候选合并流程：

- 候选评估结合地点误差、时间窗口、分类意见和观察材料四个因子，给出逐因子匹配理由、置信得分与置信等级，算法只产生候选，不做合并决定；
- 合并、不合并、拆回均由馆员决定并逐条记录理由，决定按幂等键回放，重复提交返回原结果；
- 统一观察记录保留每条原始上报、贡献者可见范围和决策时间线，可见范围取成员中最严格级别，发布的位置误差圆始终覆盖全部成员误差圆，受保护物种位置不因合并扩大披露；
- 出现新鉴定意见时候选自动重新评估，馆员可拆回旧合并并重新判断，被取代的统一记录与成员关系全程留痕；
- 查询接口可以从统一观察记录追溯到全部来源上报与鉴定意见，也能说明某个候选为何没有合并。

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
PYTHONPATH=src python3 -m field_observations.acceptance --workspace .
```

验收会建立临时 SQLite 数据库，登记馆藏环境指标、保藏库房、转运路线和材料批次，完成实验观察导入、异常复核、生物安全告警与资源分配，并回放雨后多方上报同一疑似兰科植株的候选评估、合并、拆回与不合并说明流程，输出 JSON 结果。命令不访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m collection_logistics.api --database collection.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database taxonomy.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database biosafety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m field_observations.api --database observations.sqlite3 --host 127.0.0.1 --port 8083
```

四个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可以继续查询与复核。
