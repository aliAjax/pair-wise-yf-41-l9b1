# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
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

- `station`：观测台站；`event`：地震事件及其多个修订版本；`sequence`：余震序列，按主震时空窗口归并相邻地震。

## 余震序列

- 主震按**震级最大、时间最早**确定；主震一变，序列成员立刻重算——跳出时空窗口的移出，新落进来的收进来。
- 时空窗口由 `window.max_days`（天）和 `window.max_distance_km`（震中距，Haversine）定义；事件需带 `lat`/`lon`。
- 重算按批次处理：整批失败后留下未完成批次（`batch.status = in_progress`），重试时跳过已处理成员，不重复改动。
- 发过序列公告的按原版本冻结；重算结果作为新公告版本留存，旧版本不变。
- 两名编目员提交同一序列时，后到的会收到 `409` 冲突，消息中含当前主震。
- 旧数据无序列归属，升级时按主震窗口回填（`POST /api/sequences/backfill`）。

### 序列接口

- `POST /api/sequence`：创建序列（`name`、`mainshock_id`、`member_ids`、`window`）。
- `POST /api/sequence/<id>/actions`：`action` 取 `recalculate` / `publish_announcement` / `backfill`。
- `POST /api/sequences/backfill`：全局回填无归属的地震事件。
- 事件创建或震级修订后自动重算其所属序列，无需手动触发。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
