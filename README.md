# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/calibrations`：登记校准台账（测点、设备编号、校准时间、修正系数、有效期）
- `POST /api/calibrations/recalculate`：按测点重算未结束告警
- `GET /api/calibrations?point_ref=`：查询校准台账（含已替换历史）
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 校准台账

- 按测点（`point_ref`）登记设备编号、校准时间、修正系数和有效期；同一测点仅允许一条`active`记录，重复生效返回冲突并说明现有记录，显式`replace=true`才将旧记录置为`superseded`（历史保留可查）。
- 新建告警与限行/封闭流转（`restricted`/`closed`）执行校准门禁：校准缺失、已过有效期或读数设备号与生效校准不一致时一律拒绝，并返回具体受阻原因。
- 创建告警时按生效校准把原始偏差`quantity`折算为`corrected_quantity`，并记录`calibration_id`作为结论依据；告警查询同时返回当前生效校准版本与`calibration_blockers`。
- 更换传感器后：先`replace`补录新校准，再调用重算接口；未结束告警按新系数重算（可随请求声明新读数设备号），已`restored`的告警保留历史结论不重算。校准登记、重算、查询为相互独立的接口。
- 校准登记、替换与每次重算均写入SHA-256审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
