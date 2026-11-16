---
name: ashare-gateway
description: 通过已接入的本地 A 股数据网关 MCP 查询行情、估值、财务和厂商技术指标，按需搜索指标，探索并复用个人字典。适用于使用 ashare-gateway 取数，不负责安装 MCP 或配置厂商账户。
---

# A 股数据网关

使用客户端已经连接的 `ashare-gateway` MCP。网关通常位于 `http://127.0.0.1:8765/mcp`，使用 Streamable HTTP；安装本 Skill 不会建立 MCP 连接。工具前缀随客户端变化，以当前可用工具定义为准。连接缺失时说明需要接入本机网关；不要索要厂商密钥或绕过网关另开供应商连接。

## 搜索后取数

先用 `search_metrics(query, dataset?, limit=10)` 搜索中文名、英文 ID 或别名，只读相关匹配项，不要每次加载整本字典。需要核实某个字段的取法、单位或来源口径时，用 `data_catalog(dataset, field)` 定点查询。

已有字段直接调用对应工具：

- `query_daily_prices(symbol, start_date, end_date, fields?, provider="auto", adjust="none")`：历史日线。
- `query_valuations(symbol, date?, fields?, provider="auto")`：估值与每日指标；省略日期表示最新，历史问题应传日期。
- `query_financials(symbol, period, fields?, provider="auto")`：报告期财务。
- `query_technical_indicators(symbol, start_date, end_date, fields?, provider="auto", parameters?, frequency="daily", adjust="native")`：厂商技术指标，目前支持通达信日线 KDJ 三分量。
- `provider_status()`：连接状态、本地调用统计和手动账户记录，不能当作实时官方余额。

证券代码带交易所后缀，如 `600519.SH`、`000001.SZ`；日期用 `YYYY-MM-DD`。按用户需要传少量明确字段；省略 `fields` 使用常用项，只有用户需要全量时才使用 `["*"]`。自动路由按本机设置逐字段补齐，指定来源不换源；遵守用户指定来源与复权方式。

## 未知指标：探索后保存

搜索没有合适字段时，调用 `explore_metric(name, dataset, symbol, period?/date?/start_date?/end_date?, unit?, aliases?, provider="auto", max_attempts=2, parameters?, frequency="daily", adjust?)`。名称应是一个明确数值指标；带上证券和日期，财务问题区分单季度、累计、期末或 TTM。只有确实同义的名称才作为别名，不把相似指标当作同一指标。

一般指标探索通过已配置的 Wind、iFinD、通达信自然语言工具执行；技术指标使用下面的已适配厂商工具。Tushare、AKShare 使用已有明确映射。不要猜 SDK 方法、未映射接口或参数，不因探索失败绕开网关。默认最多两个来源，每来源最多一次，`max_attempts` 支持 1–3；到达本次预算后报告缺项及原因，新增尝试需符合用户范围。

检查返回的 `status`、`candidate`、`data`、`attempts` 和证据路径：

- `existing`：已经命中字典，使用其普通查询结果。
- `success`：核对证券、实际日期、名称、数值、单位和财务口径；保留来源原生算法说明。
- `needs_confirmation` 或 `failed`：说明未确认之处，不把候选当作已核实数据，不保存。

用户要求探索并沉淀指标，或已授权维护常用字典时，成功后直接调用 `save_metric(exploration_id, field?)`，无需独立或重复审批。仅临时查询时按需提供候选。`exploration_id` 取本次结果的 `id`；自定义 `field` 用稳定的小写英文 ID，如 `net_interest_margin`。保存只增加本地可复用取法，不扩大数据源权限、不覆盖已有 ID，也不表示其他股票和年份均已验证。之后搜索和普通查询复用该 ID，不重复探索。

## 技术指标参数

`dataset="technical_indicators"` 当前适配通达信原生 KDJ，字段为 `kdj_k`、`kdj_d`、`kdj_j`；不在本地自行计算或替代厂商值。普通查询省略参数时使用字段保存的默认值，内置字段为 `{"n":9,"m1":3,"m2":3}`；自定义参数需显式传 `parameters`，每项为 1–1000 的整数。仅支持 `frequency="daily"` 和 `adjust="native"`，原生口径的复权方式未披露，不能标成“不复权”，也不能承诺前／后复权。

日期查询限于来源最近 5000 根日线覆盖内。回答时注明 K／D／J 分量、实际参数、频率、日期和来源；厂商 `params_used` 必须与请求一致，失败时不推断其实际参数。KDJ 是指标值，不是百分比收益率。

需要保存某组参数的单个分量时，例如探索 `name="KDJ J"`、`dataset="technical_indicators"`、证券和起止日期，再传 `parameters={"n":14,"m1":3,"m2":3}`。成功后按已有保存授权保存；使用返回的字段 ID，后续交给 `query_technical_indicators` 查询。不要将参数不同的指标都叫同一个 ID；本适配不代表任意技术指标均可探索。

## 回答与结果

先给用户所问的数据或结论，注明证券、实际日期／报告期、单位与来源。逐值来源以 `field_sources` 为准；同表可能混合多个来源。缺值和未确认单位如实保留，不自行补数、换算或替换口径。财务报告期数据不能据此声称是某历史时点已可获取的数据。

查询只返回前 100 行预览及完整本地结果路径；全量分析读取完整结果，不把预览当全集。需要交付完整数据时引用返回的文件或下载地址，不把大量原始表格塞入上下文。不要读取、回传或修改凭证配置，也不要借保存指标修改路由、来源开关或账户权限。
