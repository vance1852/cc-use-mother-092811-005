# 综合枢纽无障碍旅客接续协助平台

面向轮椅等无障碍旅客在综合枢纽内跨铁路、城市客运、站内服务队接力换乘的协同服务。
平台把**行程版本、设备条件、交接位置、各单位值守能力**编成一条不可断裂的服务链：
每段必须经承接单位接受并锁定资源，交接采用两阶段确认，晚点/改签/设备故障/旅客失约/
紧急人工接管均有确定的重排与升级规则，需求按最小披露原则分发并全程留痕。

## 核心规则

- **分段接受才锁定资源**：区段状态为 `proposed → accepted → in_progress → completed`；
  承接时校验单位在登车点/交接点的值守窗口、设备类型与时间冲突，能力不足拒绝承接。
- **两阶段交接，一次且仅一次**：送出方先 `arrive`（旅客带到交接点），接方再 `receive`
  完成交接并自动开工下一段；重复回执返回冲突，同一 `request_id` 重放只返回原回执。
- **晚点/改签版本化重排**：每次变更生成新行程版本；在途段延续（设备锁随之迁移或转
  `resourcing`），后续段一律回到待承接并按 10 分钟承接时限升级，旧计划统一 `superseded`。
- **设备故障**：自动寻找同点替代设备；无替补时区段转 `resourcing` 并一级升级（10 分钟
  处置时限，超时升二级）。
- **旅客失约**：服务链挂起 `no_show`、释放未完成资源并二级升级；旅客返回后开新版本恢复。
- **紧急人工接管**：任意现场人员可触发，现场操作立即冻结，三级升级直达协调员，指派负责人
  并处置后方可解除。
- **超时升级**：交接点宽限 5 分钟、接方确认宽限 3 分钟、首段开工宽限 5 分钟；一级未处置
  10 分钟后升二级，紧急为三级。
- **最小披露**：需求分 `general`/`health`；健康类必须显式指定可见区段，任何单位只能取阅
  本单位承接区段的需求，且仅被指派责任人可取阅明细；每次取阅写入披露记录。
- **不可篡改**：已完成段与交接标记 `immutable`，改线后作为原始记录永久保留；所有动作进入
  哈希串联审计链。
- **旅客核验**：建档时设置只存哈希的旅客令牌，旅客凭令牌查看全链并核对“谁在何时访问过
  哪些需求”。

## 目录

- `src/transport_coordination/`
  - `assistance.py`：接续链领域规则（承接、交接、版本重排、失约、紧急接管、升级扫描、最小披露）
  - `service.py` / `domain.py`：组织、人员、场所、资料登记与角色权限
  - `storage.py`：SQLite 表结构、事务与资源锁部分唯一索引
  - `audit.py` / `clock.py`：哈希审计链与可替换时钟
  - `api.py`：标准库 HTTP/JSON 边界
  - `acceptance.py`：离线端到端验收
- `tests/`：存储、服务、接续规则、HTTP 路由与离线验收测试

## 环境

- Linux，Python 3.11+，仅依赖标准库与 SQLite。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收覆盖：建档 → 三段承接锁资源 → 首段交接 → 第 2 段晚点重排 → 重新承接 → 完成全链 →
最小披露留痕核对，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 0 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database service.sqlite3 \
  --host 127.0.0.1 --port 8080
```

写接口与值班接口通过 `X-Actor-Id` 标识操作者；旅客接口通过 `X-Passenger-Token`
（或 `?token=`）核验。主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/capabilities` | 单位声明值守窗口、交接点与设备 |
| POST | `/equipment-incidents` `/equipment-incidents/{id}/resolve` | 设备故障上报/关闭 |
| POST | `/chains` | 旅客或协调员提交服务链与需求 |
| POST | `/chains/{id}/segments/{n}/accept` `/start` | 区段承接（锁资源）/开工 |
| POST | `/chains/{id}/handovers/{b}/arrive` `/receive` | 两阶段交接 |
| POST | `/chains/{id}/delay` `/rebook` | 晚点 / 改签重排（新版本） |
| POST | `/chains/{id}/no-show` `/resume` | 旅客失约挂起 / 返回恢复 |
| POST | `/chains/{id}/emergency` `/emergency/owner` `/emergency/resolve` | 紧急接管与处置 |
| GET | `/chains/{id}` | 协调员视图（无健康明细）或旅客全量视图（凭令牌） |
| GET | `/chains/{id}/needs?segment_seq=n` | 本单位责任人取阅本段需求（留痕） |
| GET | `/chains/{id}/access-log` | 旅客核对需求访问记录（凭令牌） |
| GET | `/chains/{id}/history` | 已完成段与交接的原始记录 |
| GET | `/duty-board` | 当前责任人、下一次交接与超时风险 |
| POST | `/sweeps` | 触发确定性超时扫描与升级 |

所有写接口均要求 `request_id` 做幂等键；同键不同内容返回 `409`。
