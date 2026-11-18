---
name: ashare-gateway
description: 通过渡数 MCP 查询 A 股数据，搜索字段、探索未知数值指标并复用个人字典。
---

# 渡数 MCP

使用已连接的 `ashare-gateway`；传输为 Streamable HTTP，默认地址 `http://127.0.0.1:8765/mcp`，以用户提供的地址为准。Skill 是使用说明，不会自动建立连接。无需向 Agent 提供厂商密钥。

## 工具

| 工具 | 用途 |
| --- | --- |
| `search_metrics` | 按中文名、字段 ID 或别名找指标；支持 dataset 筛选 |
| `data_catalog` | 按 dataset、field 查看定义、单位、来源取法与参数 |
| `query_basic_info` | 公司档案、名称历史、行业与板块；无日期参数 |
| `query_daily_prices` | 起止日期内的日线；支持复权 |
| `query_valuations` | 指定交易日估值；省略日期查最新 |
| `query_financials` | 指定报告期的财务数据 |
| `query_technical_indicators` | 技术指标；按字段定义选择日期、参数与复权 |
| `explore_metric` | 探索字典未覆盖的 A 股数值指标 |
| `save_metric` | 手动保存成功候选；自动保存成功后无需调用 |
| `provider_status` | 来源连接状态、本地调用统计及账户记录 |

## 用法

1. 先搜索字段，必要时用 `data_catalog(dataset, field)` 定点查定义，不加载整本字典。dataset 为 `basic_info`、`daily_prices`、`valuations`、`financials`、`technical_indicators`；具体参数以工具 schema 为准。
2. 调用对应查询工具，传证券代码（如 `600519.SH`）、所需日期（`YYYY-MM-DD`）和少量 `fields`。`provider="auto"` 按用户路由补缺；指定来源不换源。技术指标的周期、单位与复权按字典取，不自行假设。
3. 未知数值指标用 `explore_metric`。成功默认自动保存个人字典，核对 `saved` 后告知用户；有歧义再问。仅临时查询传 `auto_save=false`。文本和列表暂不支持探索。
4. 回答注明实际来源、日期和单位，保留缺值。按 `field_sources` 识别混合来源；检查 `coverage`，保存成功不等于历史取全。超过100行时读取返回的完整结果文件，不把预览当全集。基本信息是当前档案，不能冒充历史时点资料。
