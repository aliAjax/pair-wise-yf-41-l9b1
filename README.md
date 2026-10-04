# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/sequences.py`：余震序列的主震选择、时空窗口和成员判定（纯函数，无 IO）。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `sequence`：余震序列（人工挂接的锚点事件 + 按主震窗口算出的成员）。
- `sequence_notice`：序列公告版本快照，一经发布不可改写。

## 余震序列规则

- 主震 = 锚点事件中**震级最大、并列时时间最早**的事件（`src/sequences.py`）。
- 主震一换立即对全目录重开时空窗口：掉出窗口的成员移出，新落入窗口的事件收入；成员表只做差集写入，已是目标状态的成员不重复改动。
- 窗口可由序列显式给出（`window_days`、`window_distance_km`），否则按主震震级查简化的 Gardner-Knopoff 对照表。
- 发布过公告的序列按原版本冻结；对冻结序列再做挂接或重算时，旧公告保留，新结果生成下一版 `sequence_notice`。
- 两名编目员并发提交：同名序列创建直接 409 并回传当前主震；基于旧版本号的挂接/重算触发乐观锁 409，响应体 `details` 带当前版本号和当前主震。
- 整批重算：`POST /api/sequences/recompute`。单项失败只标记该条，批次整体置为 `partial`；`POST /api/sequences/<batch_id>/retry` 只重试未完成项，已改过的成员不会重复修改。
- 旧数据升级：`POST /api/sequences/backfill`（仅 admin），对没有任何序列归属的历史事件按主震窗口贪心聚簇回填，缺时间/坐标/震级的事件跳过；已有归属的事件不会被二次挂列。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本（序列会附带成员事件与公告版本列表）。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  - 序列动作：`attach`（挂入新锚点，立即重算）、`recompute`（可在 data 中覆盖窗口）、`publish`（发布公告并冻结）、`unfreeze`。
- `POST /api/sequences/recompute`：整批重算，可传 `{"sequence_ids":[...]}`，返回批次状态。
- `POST /api/sequences/<batch_id>/retry`：重试批次中未完成/失败的序列。
- `GET /api/sequences/batch?id=<batch_id>`：查询批次明细。
- `POST /api/sequences/backfill`：旧数据序列回填。
- `GET /api/sequences/<id>/notices`：读取序列的全部公告版本。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
