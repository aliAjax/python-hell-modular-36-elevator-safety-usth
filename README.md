# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据、恢复许可、当日计划和复检排程。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。角色包括`viewer`、`dispatcher`（调度员）、`inspector`（检验员）、`senior_inspector`（主任检验员）、`maintenance`和`admin`。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 当日计划与复检排程

设备、困人报警、救援任务和复检排程共用一个按日期唯一的`daily_plan`（当日计划），计划内用`reinspection_capacity`限制当天复检名额：

- `POST /api/daily_plans`：创建当日计划，字段为`plan_date`（`YYYY-MM-DD`）和`reinspection_capacity`，仅`admin`/`dispatcher`可创建，同一天不能重复建计划。
- `POST /api/reinspections`：提交排检`{"plan_id","equipment_id"}`。排检和占名额在单个`BEGIN IMMEDIATE`事务内完成，两名调度员并发提交同一台电梯时，后到者收到409冲突，错误信息带已存在排检的id和状态（scheduled/queued）。
- 排序规则（靠前者拿名额）：①无未解除报警/未结束救援的在前；②高风险先于中、低风险（设备`risk_level`，默认`low`）；③检验已逾期 > 当天到期 > 未到期 > 从未通过检验；④到期日更早；⑤更早提交。容量满时低优先级排检进入`queued`排队。
- `GET /api/daily_plans/<id>/board`：当日看板，含已占用名额、已排（scheduled）、排队（queued）、已结束（passed/failed/voided），以及每条排检关联的设备、未解除报警和未结束救援。
- `POST /api/daily_plans/<id>/rerank`：手动重排（报警解除、救援结束、复检签收或作废时系统也会自动重排并退回名额）。
- 设备状态发生任何变化（停用、暂停、恢复）时，该设备已排/排队中的复检自动`voided`作废、名额退回并把队首提升上来，审计记录中带`"auto": true`。
- 高风险电梯的复检签收（pass/fail）仅`senior_inspector`或`admin`可执行，普通`inspector`签低风险可以、签高风险返回403。
- 复检合格前恢复许可继续冻结：存在scheduled/queued复检，或最近一次复检为failed时，许可的`request_review`和`grant`都被拒绝。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验（含通过的复检）和未关闭整改共同限制，且复检未完成或最近不合格时持续冻结。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
