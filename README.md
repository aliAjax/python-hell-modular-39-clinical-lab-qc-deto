# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换、桥接覆盖和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准、放行约束和桥接规则。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、桥接唯一键和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制、失控退回和旧数据升级。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`qc_bridge`为桥接覆盖。

## 桥接覆盖

换质控品批次后靶值和标准差改变，患者结果批次可借旧批号放行。桥接按`仪器:检测项目:质控品批次`建覆盖（`bridge_key`唯一），借用旧批号靶值与标准差，状态机为`pending → confirmed → expired/stopped`。

- `confirm`/`reconfirm`/`expire`/`revoke`：仅`supervisor`/`admin`可操作；`reconfirm`需新的`valid_until`。
- `rollback`：新批号失控时停止放行，只把`cutoff_at`之后放行的批次退回为`returned`，切点之前的批次保持放行。
- 并发确认：唯一`bridge_key`索引加乐观锁，两个操作员同时提交只产生一份生效版本。
- 断点续跑：退回失败后重试自动跳过已退回批次，不新增退回审计记录。
- 旧数据升级：`POST /api/migrate/bridges`把缺少`bridge_id`的已放行批次标为`pending_bridge`，不能沿用旧结论；幂等可重复执行。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/migrate/bridges`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
