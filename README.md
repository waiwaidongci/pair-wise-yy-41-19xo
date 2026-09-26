# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和校准闸门规则。
- `src/repository.py`：SQLite建表、事务、版本控制、校准台账和审计链。
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
- `POST /api/items`，可带`point_code`（测点）、`device_id`（设备编号）、`quantity`（原始读数）；放行前校验校准台账
- `GET /api/items/{id}`，返回`calibration`段：生效校准编号、系数、有效期、`blockers_create`、`blockers_restriction`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/calibrations?point_code=`：校准台账查询（登记/重算/查询三分开）
- `GET /api/calibrations/{id}`
- `POST /api/calibrations`：登记校准，字段`point_code/device_id/calibrated_at/valid_from/valid_until/correction_factor/replace/note`
- `POST /api/calibrations/recalculate`：独立重算用例，可带`point_code`；未结束告警按新生效系数重算，历史结论保留
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 校准台账规则

- 按测点登记：设备编号、校准时间、修正系数、有效期（`valid_from`/`valid_until`），每次登记与替换均写审计链。
- 同一测点同一时刻只允许一份生效记录。登记与既有生效区间重叠时返回409冲突并列出冲突编号；显式传`replace=true`可将旧记录置为`superseded`后再登记。台账查询对重复生效记录标注冲突编号。
- 校准闸门：新告警（`POST /api/items`，绑定测点时）以及转向`restricted`/`closed`的判断，在校准缺失、过期、设备号对不上、重复生效时一律不得放行；限行判断还要求告警当前依据的校准编号就是生效版本，否则须先重算。
- 补录合格校准后，调用独立重算接口：未结束告警的修正读数按“原始读数×新系数”重算并绑定新生效版本（换设备时同步更新设备号）；已结束（`restored`）告警不动，历史结论、旧校准记录与审计痕迹全部保留。
- 登记（sensor_operator/bridge_engineer）、重算（bridge_engineer）、查询（全部角色）权限分离；页面展示每条告警的生效版本与受阻原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
