---
name: ashare-gateway
description: 通过渡数 MCP 查询 A 股数据；字典未命中时先探索上游能力，保存成功取法并复用。
---

# 渡数 MCP

使用已连接的 `ashare-gateway`，Streamable HTTP，默认 `http://127.0.0.1:8765/mcp`，以用户提供的地址为准。Skill 不会自动建立连接；无需索要厂商密钥。

## 调用顺序

1. 用 `search_metrics` 找常用字段或个人取法。只有含义匹配才复用；相近字段不能替代用户所要的名单、范围或口径。需要时用 `data_catalog(dataset, field)` 定点查定义，不读整本字典。
2. **没有精确匹配，先在网关内探索：未知数值指标且类别明确时用 `explore_metric`；其他需求用 `explore_data`，提交原始需求、证券和时间，不必猜类别。** 需要先了解来源时用 `search_capabilities`。字典未收录不等于供应商没有，不能因此直接转到网上查。
3. 探索成功查看 `saved`：`saved_query_id` 用 `query_data` 复用，`saved_field` 用对应类别工具查询。告知用户是否已保存；歧义先澄清，临时查询传 `auto_save=false`。只有对应的网关探索明确失败后才考虑外部搜索；数值需求不能因 `explore_data` 未适配而跳过 `explore_metric`。
4. `needs_confirmation` 不等于无数据：有 `data` 或 `candidates` 就向用户展示并标注疑点、实际来源和日期，用户或 Agent 可核对后接受；不要称为已核实，也不要仅因未自动保存就弃用或反复付费重试。
5. 回答注明实际来源、日期和单位，保留缺值。查看 `coverage` 与完整结果路径，不把前100行预览、搜索片段或保存成功说成已经取全。基本信息当前档案不能冒充历史时点资料。

## 工具

| 工具 | 用途 |
| --- | --- |
| `search_metrics` | 搜常用字段和个人取法；返回 `query_tool` / `query_id` 时按提示复用 |
| `search_capabilities` | 搜上游工具能力，区分已适配、尚未适配和未验证 |
| `explore_data` | 不指定类别探索原始需求；支持已适配的记录表，成功默认保存取法 |
| `query_data` | 按 `query_id` 复用个人取法，可更换证券和时间 |
| `data_catalog` | 查字段定义、单位、来源与参数 |
| `query_basic_info` | 公司档案、名称历史、行业与板块 |
| `query_daily_prices` | 日期区间内的日线与复权 |
| `query_valuations` | 指定交易日估值，省略日期查最新 |
| `query_financials` | 指定报告期财务 |
| `query_technical_indicators` | 按字典定义的周期、日期和复权查技术指标 |
| `explore_metric` / `save_metric` | 已明确类别的数值指标探索、手动保存候选 |
| `provider_status` | 来源连接状态、本地统计与账户记录 |

证券代码如 `600519.SH`，日期用 `YYYY-MM-DD`。`provider="auto"` 沿用户偏好，指定来源不换源；参数以工具 schema 为准。实际记录形态和可调用范围以能力搜索、探索返回为准，不把通用入口理解为所有类型均已适配。
