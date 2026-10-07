# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换、桥接放行和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`lot_bridge`为批号桥接覆盖。

## 桥接放行

新质控品批号（新靶值/标准差）切换进来后，患者结果继续借旧批号放行的前提是存在有效桥接：

- 桥接按**仪器 + 检测项目 + 新质控品批号**建立（`coverage_key`），并指向被借用的旧批号；初始为`pending`。
- `confirm`需授权人（`supervisor`/`admin`）确认，确认后为`confirmed`才能承接放行；放行时在结果批次上写入`release_mode=bridge`、`bridge_id`、`bridge_version`与`bridge_key`。
- 同一覆盖键下只有一份生效版本：`bridge_coverage`表保证两个操作员并发确认只产生一份，失败重试直接返回该版本，不新增审计/版本记录。
- 桥接超过`expires_at`即失效，动作`expire`将其置为`expired`并拒绝放行；`reconfirm`（需新的有效期与授权人）后再次`confirm`即重新确认覆盖。
- 新批号质控失控时，对桥接执行`stop`（`cutoff_at`/`reason`/`failure_run_id`）：桥接置为`suspended`，只把切断点**之后**的相关待放行/已放行批次退回（`intercept`/`recall`），切断点及之前的结果不动。`bridge_stop_checkpoint`保证失败后从断点重试，不新增退回记录。
- 旧数据缺少桥接键：`POST /api/system/upgrade-legacy-batches`将跨批号切换后仍为已放行的批次升级为`pending_bridge`（保留`pre_upgrade_status`），不能沿用旧放行结论，必须由有效桥接重新放行；升级操作幂等。

`lot_bridge`动作：`confirm`、`expire`、`reconfirm`、`stop`。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
