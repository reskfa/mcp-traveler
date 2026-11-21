---
name: ashare-gateway
description: 通过羊毛伞 MCP 查询 A 股与可转债数据；字典未命中时先探索上游能力，保存成功取法并复用。
---

# 羊毛伞 MCP

使用已连接的 `ashare-gateway`，Streamable HTTP，默认 `http://127.0.0.1:8765/mcp`，以用户提供的地址为准。Skill 不会自动建立连接；无需索要厂商密钥。

## 调用顺序

1. **跨券或全市场行情先用专用市场工具**：名单用 `query_market_universe`，多券／全市场日线用 `query_market_daily`，区间收益排名用 `query_market_returns`，不要循环旧单券工具。`symbols=None`（省略）才是全市场，空数组无效。
2. 单券及其他需求用 `search_metrics` 找常用字段或个人取法。只有含义匹配才复用；相近字段不能替代用户所要的名单、范围或口径。需要时用 `data_catalog(dataset, field)` 定点查定义，不读整本字典。
3. **没有精确匹配，先在网关内探索：未知数值指标且类别明确时用 `explore_metric`；其他需求用 `explore_data`，提交原始需求、证券和时间，不必猜类别。** 无代码市场探索省略 `symbol`，并传 `query_scope="market"`、`market_hint="equity|convertible_bond"`、`market_kind="universe|daily"`；名单历史时点用 `end_date`。需要先了解来源时用 `search_capabilities`，传相同 scope。字典未收录不等于供应商没有，不能因此直接转到网上查。
4. 探索成功查看 `saved`：`saved_query_id` 用 `query_data` 复用，`saved_field` 用对应类别工具查询。市场取法复用时不填 `symbol`，可换日期；保存的是取法，不是代码清单。告知用户是否已保存；歧义先澄清，临时查询传 `auto_save=false`。只有对应的网关探索明确失败后才考虑外部搜索；数值需求不能因 `explore_data` 未适配而跳过 `explore_metric`。
5. `needs_confirmation` 不等于无数据：有 `data` 或 `candidates` 就向用户展示并标注疑点、实际来源和日期，用户或 Agent 可核对后接受；不要称为已核实，也不要仅因未自动保存就弃用或反复付费重试。
6. 回答注明实际来源、日期和单位，保留缺值。查看 `coverage` 与完整结果路径，不把前100行预览、搜索片段或保存成功说成已经取全。市场探索响应不裁100行，但仍须看上游总数与截断证据；基本信息当前档案不能冒充历史时点资料。

可转债与股票分开：代码如 `113050.SH`、`123001.SZ`。用 `cb_basic` 搜当前档案，`cb_daily` 搜历史行情与估值，`cb_clauses` 搜条款公告事件。未知指标或名单用 `explore_data(..., market_hint="convertible_bond")`，能力搜索也传该提示；不拿股票估值或财务接口试可转债。个人取法按证券类型保存，不能跨市场套用。条款公告日期不是历史时点状态，当前转股价和余额也不能回填历史。

## 工具

| 工具 | 用途 |
| --- | --- |
| `query_market_universe` | 当前或历史时点的全市场 A 股／转债名单 |
| `query_market_daily` | 显式多代码或全市场日线；默认完整 JSON，`top_n` 仅裁剪响应 |
| `query_market_returns` | 同源区间价格收益及稳定排名；检查 `ranking_complete` 和排除原因 |
| `search_metrics` | 搜常用字段和个人取法；返回 `query_tool` / `query_id` 时按提示复用 |
| `search_capabilities` | 搜上游工具能力，区分已适配、尚未适配和未验证 |
| `explore_data` | 不指定类别探索原始需求；支持已适配的记录表，成功默认保存取法 |
| `query_data` | 按 `query_id` 复用个人取法，可更换证券和时间 |
| `data_catalog` | 查字段定义、单位、来源与参数 |
| `query_convertible_bond_basic` | 可转债基本信息当前档案 |
| `query_convertible_bond_daily` | 可转债日期区间行情及估值 |
| `query_convertible_bond_clauses` | 可转债条款公告事件，不重建历史状态 |
| `query_basic_info` | 公司档案、名称历史、行业与板块 |
| `query_daily_prices` | 日期区间内的日线与复权 |
| `query_valuations` | 指定交易日估值，省略日期查最新 |
| `query_financials` | 指定报告期财务 |
| `query_technical_indicators` | 按字典定义的周期、日期和复权查技术指标 |
| `explore_metric` / `save_metric` | 已明确类别的数值指标探索、手动保存候选 |
| `provider_status` | 来源连接状态、本地统计与账户记录 |

近 5 个交易日跌幅最大的转债：

```text
query_market_returns(scope="convertible_bond", end_date="2026-11-20",
  trading_days=5, order="asc", top_n=10)
```

窗口包含结束日在内的 5 个交易日，另取前一交易日收盘价为基准。股票使用同源前复权，转债使用同源不复权；缺失、停牌无行、失败或混合来源不当作 0，也不进入排名。`ranking_complete=false` 时只能说明是合格输入中的 Top N，不能称为完整全市场排名。

证券代码如 `600519.SH`，日期用 `YYYY-MM-DD`。`provider="auto"` 沿用户偏好，指定来源不换源；参数以工具 schema 为准。实际记录形态和可调用范围以能力搜索、探索返回为准，不把通用入口理解为所有类型均已适配。
