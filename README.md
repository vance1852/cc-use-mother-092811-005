# 综合枢纽无障碍旅客接续协助协同平台

轮椅旅客在综合枢纽换乘时，由铁路、城市客运、站内服务队等多家单位分段接力护送。
本平台在既有基础登记服务之上，把**行程版本、设备/人员值守能力、交接位置和各单位
责任**编成一条不可断裂的服务链，并提供：

- **接单锁定**：每段必须先接受并锁定所需设备/人员资源，链才成立；
- **双签交接**：交出方带旅客到达交接位置、接入方确认接走，两次动作齐备才完成交接，
  任何一方都不能单独关闭工单；
- **确定性重排**：晚点、改签、设备故障、旅客失约、紧急人工接管都有明确的版本化重排与
  升级规则；已完成段在新版本中冻结复制，原始行永不改写；
- **最小披露**：每条需求带可见范围（全链 / 指定段 / 指定单位），医疗类需求禁止全链可见，
  单位只能查看与本段相关的需求，每次披露都写入访问履历；
- **幂等回执**：所有写操作要求 `request_id`，重复回执回放原始结果，不会完成两次交接；
- **值班视图**：看板给出当前责任人、下一次交接与超时风险；旅客可核对谁在何时访问过哪些需求。

## 目录

- `src/transport_coordination/`
  - `storage.py`：SQLite 建表与事务边界；
  - `service.py`：组织、操作者、场所、领域资料的登记与权限；
  - `relay.py`：接续协助服务链、重排引擎、最小披露、升级与看板；
  - `audit.py`：哈希串联审计日志；
  - `api.py`：仅依赖标准库的 HTTP/JSON 边界；
  - `acceptance.py` / `relay_acceptance.py`：两套离线端到端验收。
- `tests/`：基础服务、接力领域规则、HTTP 路由与验收测试。

## 环境

- Linux，Python 3.11+，运行时仅使用 Python 标准库与 SQLite。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m transport_coordination.relay_acceptance
```

`relay_acceptance` 复现“铁路首段晚点、后续单位按原时刻到场各自关单、旅客到转乘口无人接应”
的故障场景，验证双签交接、确定性晚点重排、已完成段原始记录保留、最小披露、幂等与访问履历，
成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 \
  --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，写操作体必须带 `request_id`；重启后 SQLite 中的业务
状态、行程版本与审计历史继续保留。

## 角色

| 角色 | 职责 |
| --- | --- |
| `passenger` | 旅客本人，提交需求、建链、确认送达、核对访问履历 |
| `operator` | 各运营单位值守人员，管理本段资源、接单、护送、交接 |
| `coordinator` | 枢纽无障碍协调员，改签、人工接管、处置升级、全局看板 |
| `admin` / `auditor` | 管理员 / 审计员 |

## 接续协助接口

值守能力与代理：

- `POST /relay/resources`：登记单位在某场所、某时段、某类设备/人员的值守能力；
- `POST /relay/agent-grants`：旅客向授权代理授予代理权。

服务链：

- `POST /assistances`：旅客或授权代理提交需求并建立服务链；
- `GET /assistances/{id}?version=`：查询行程（可指定历史版本；运营单位只看到本单位段）；
- `POST /legs/{id}/accept` / `decline` / `start` / `assign-resource` /
  `no-show` / `no-show-recover` / `delivered` / `delay`；
- `GET /legs/{id}/needs`：按最小披露返回本段可见需求并留痕；
- `POST /handoffs/{id}/arrive`：交出方报告带旅客到达交接位置；
- `POST /handoffs/{id}/receive`：接入方确认接走（与到达双签后交接完成）；
- `POST /resources/{id}/failure`：报告设备故障，自动重排并在无替代能力时升级紧急指挥；
- `POST /assistances/{id}/ticket-change`：改签重排；
- `POST /assistances/{id}/takeover` / `takeover-resume`：紧急人工接管与解除；
- `POST /assistances/{id}/complete`：末段送达后由旅客/代理/协调员确认；
- `POST /timeouts/sweep`：按当前时钟扫描接单、到达、交接、送达超时并产生确定性升级；
- `POST /escalations/{id}/resolve` / `GET /escalations`：升级处置与查询；
- `GET /board`：值班员看板（当前责任人、下一次交接、超时风险，单位视角自动收窄）；
- `GET /assistances/{id}/access-history`：旅客核对谁在何时访问过哪些需求。

## 需求可见范围

每条需求声明 `visibility.scope`：

- `chain`：全链各段可见（医疗类需求禁止使用）；
- `leg`：仅 `ordinals` 指定的段可见；
- `unit`：仅 `organization_ids` 指定的单位可见。

需求明细永不随看板或行程查询返回，只有承担该段的单位通过 `GET /legs/{id}/needs` 主动取阅时
才会披露，并同时写入 `access_log` 与哈希审计链。

## 状态与重排规则摘要

- 段状态：`offered → accepted → in_progress → completed`，另有 `delivered`（末段待确认）、
  `no_show`、`taken_over`；交接状态：`pending → arrived → completed`。
- 晚点：报告段及其后所有未完成段顺延；已完成段冻结原时刻，进行中段随旅客延续，
  未接手段回到 `offered` 按新版本重新锁定资源。
- 改签：不得改变任何已完成段的单位、位置与时刻。
- 设备故障：设备标记 `faulty`，进行中锁清空并等待改派；无替代能力时升级三级紧急指挥。
- 旅客失约：段置 `no_show` 并升级；旅客找回后可恢复护送。
- 紧急人工接管：全链单位动作暂停；协调员编排新版本后解除接管。
- 超时：过接单截止、过到达/交接/送达时间按 10/30 分钟阈值升级二级或三级。
