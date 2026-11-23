---
name: ashare-gateway
description: 通过羊毛伞 MCP 查询 A 股与可转债数据；字典未命中时先探索上游能力，保存成功取法并复用。
---

# 羊毛伞 MCP

使用已连接的 `ashare-gateway`，Streamable HTTP，默认 `http://127.0.0.1:8765/mcp`，以用户提供的地址为准。Skill 不会自动建立连接；无需索要厂商密钥。

## 调用顺序

1. **跨券或全市场行情先用专用市场工具**：名单用 `query_market_universe`，多券／全市场日线用 `query_market_daily`，区间收益排名用 `query_market_returns`，不要循环旧单券工具。`symbols=None`（省略）才是全市场，空数组无效。`provider="auto"` 的市场查询优先原生全量／批量能力，不沿用旧单券逐券优先级。
2. 单券及其他需求用 `search_metrics` 找常用字段或个人取法。只有含义匹配才复用；相近字段不能替代用户所要的名单、范围或口径。需要时用 `data_catalog(dataset, field)` 定点查定义，不读整本字典。
3. **没有精确匹配时，统一使用多源探针 `explore_field`**：直接传入需求描述、证券（单券 `symbol` / 多券 `symbols` / 省略即全市场）与时间参数。`explore_field` **不重查字典、不早停**，按路由受控并发探查全部可用来源（4 家 MCP + Tushare Pro 目录 + AKShare 本地索引）。若想指定某来源的工具或 SDK 接口，传 `tool_overrides={"tushare": "cb_daily"}`。
4. **预览 + `describe` + 本地 CSV（防爆 Token）**：`explore_field`、`query_market_cross_section`、`query_market_time_series`、`helper_market_symbols` 在大结果时只返回前 20~50 行预览 + `describe`（总行数、列数、日期范围、非空统计、数值均值/极值）+ 完整本地 **`csv_path`**。需要做统计计算（均值、排名、汇总、画图）时，**直接用 Python/`pandas.read_csv(csv_path)` 读取本地 CSV**，不要在上下文中硬塞全市场裸 JSON。
5. **由 Agent 汇总口径并决定入库**：收到 `explore_field` 对比包后，横向比较各家来源的列名、单位、数值与口径差异，自行判断或请用户拍板；确认后调用 `save_explored_field(exploration_id, label, sources=[...])`，**一次将一家或多家来源同时写入个人字典**，后续走常规查询自动补齐复用。
6. 回答注明实际来源、日期和单位，保留缺值。查看 `coverage` 与完整结果路径，不把前100行预览、搜索片段或保存成功说成已经取全。市场探索响应不裁100行，但仍须看上游总数与截断证据；基本信息当前档案不能冒充历史时点资料。

可转债与股票分开：代码如 `113050.SH`、`123001.SZ`。用 `cb_basic` 搜当前档案，`cb_daily` 搜历史行情与估值，`cb_clauses` 搜条款公告事件。未知指标或名单直接用 `explore_field(..., scope="convertible_bond")`，不拿股票估值或财务接口试可转债。个人取法按证券类型保存，不能跨市场套用。条款公告日期不是历史时点状态，当前转股价和余额也不能回填历史。

## 工具

| 工具 | 用途 |
| --- | --- |
| `helper_trade_calendar` | 辅助函数：拉取 A 股交易日历（多源路由，优先 Tushare/AKShare 原生，回退各 MCP 上证指数日线，不锁定 Tushare） |
| `helper_market_symbols` | 辅助函数：拉取全市场 A 股／可转债代码列表（返回 `symbols` 数组及明细，支持多源探测与截断标注） |
| `helper_search_securities` | 辅助函数：按名称/简称或代码双向搜索 A 股与可转债（名称搜代码 / 代码搜名称），多源自动路由 |
| `helper_capabilities` | 辅助函数：查看六家数据源在交易日历、全市场代码与搜索证券上的实测能力矩阵与限制 |
| `query_market_cross_section` | 截面数据：指定日期，多券或全市场 × 一个或多个指标；返回 `index=codes, columns=fields` 及能力矩阵 |
| `query_market_time_series` | 时序数据：日期区间（日/周/月频），多券或全市场 × 单个指标；返回 `index=dates, columns=codes` 及能力矩阵 |
| `market_panel_capabilities` | 查看六家数据源在多券/全市场取数上属于一次性全市场、多券批量还是单券轮询 |
| `query_market_universe` | 当前或历史时点的全市场 A 股／转债名单 |
| `query_market_daily` | 显式多代码或全市场日线；默认完整 JSON，`top_n` 仅裁剪响应 |
| `query_market_returns` | 同源区间价格收益及稳定排名；检查 `ranking_complete` 和排除原因 |
| `search_metrics` | 搜常用字段和个人取法；返回 `query_tool` / `query_id` 时按提示复用 |
| `search_capabilities` | 搜上游工具能力，区分已适配、尚未适配和未验证 |
| `explore_field` | 统一多源探针：并发查全六家来源（MCP+SDK），返回预览、`describe` 与本地 `csv_path` |
| `save_explored_field` | 基于 `exploration_id` 一次将一家或多家来源入库到个人字典 |
| `sync_sdk_catalog` | 刷新 Tushare 官方 skills 目录与本地 AKShare 函数索引 |
| `query_data` | 按 `query_id` 复用已保存的记录型个人取法，可更换证券和时间 |
| `data_catalog` | 查字段定义、单位、来源与参数 |
| `query_convertible_bond_basic` | 可转债基本信息当前档案 |
| `query_convertible_bond_daily` | 可转债日期区间行情及估值 |
| `query_convertible_bond_clauses` | 可转债条款公告事件，不重建历史状态 |
| `query_basic_info` | 公司档案、名称历史、行业与板块 |
| `query_daily_prices` | 日期区间内的日线与复权 |
| `query_valuations` | 指定交易日估值，省略日期查最新 |
| `query_financials` | 指定报告期财务 |
| `query_technical_indicators` | 按字典定义的周期、日期和复权查技术指标 |
| `provider_status` | 来源连接状态、本地统计与账户记录 |

近 5 个交易日跌幅最大的转债：

```text
query_market_returns(scope="convertible_bond", end_date="2026-11-20",
  trading_days=5, order="asc", top_n=10)
```

窗口包含结束日在内的 5 个交易日，另取前一交易日收盘价为基准；网关只取基准日和结束日两次全市场收盘价，不取窗口中间行情。股票使用同源前复权，转债使用同源不复权；缺失、停牌无行、失败或混合来源不当作 0，也不进入排名。`ranking_complete=false` 时只能说明是合格输入中的 Top N，不能称为完整全市场排名。

市场日期自动对齐：日期范围的非交易日开始日顺延、结束日提前；单日的非交易日优先取前一交易日。`query_market_returns` 同样顺延开始日、提前结束日。原请求与实际日期见 `window`，回答必须使用实际交易日。

证券代码如 `600519.SH`，日期用 `YYYY-MM-DD`。`provider="auto"` 沿用户偏好，指定来源不换源；参数以工具 schema 为准。实际记录形态和可调用范围以能力搜索、探索返回为准，不把通用入口理解为所有类型均已适配。
