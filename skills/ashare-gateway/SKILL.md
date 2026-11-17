---
name: ashare-gateway
description: 通过已接入的本地 A 股数据网关 MCP 查询行情、估值、财务和厂商技术指标，按需搜索指标，探索并复用个人字典。适用于使用 ashare-gateway 取数，不负责安装 MCP 或配置厂商账户。
---

# A 股数据网关

使用客户端已经连接的 `ashare-gateway` MCP。网关通常位于 `http://127.0.0.1:8765/mcp`，使用 Streamable HTTP；安装本 Skill 不会建立 MCP 连接。工具前缀随客户端变化，以当前可用工具定义为准。连接缺失时说明需要接入本机网关；不要索要厂商密钥或绕过网关另开供应商连接。

## 搜索后取数

先用 `search_metrics(query, dataset?, limit=10)` 搜索中文名、英文 ID 或别名，只读相关匹配项，不要每次加载整本字典。需要核实某个字段的取法、单位或来源口径时，用 `data_catalog(dataset, field)` 定点查询。

已有字段直接调用对应工具：

- `query_daily_prices(symbol, start_date?, end_date?, fields?, provider="auto", adjust="none")`：历史日线。
- `query_valuations(symbol, date?, fields?, provider="auto")`：估值与每日指标；省略日期表示最新，历史问题应传日期。
- `query_financials(symbol, period, fields?, provider="auto")`：报告期财务。
- `query_technical_indicators(symbol, start_date?, end_date?, fields?, provider="auto", parameters?, frequency="daily", adjust="native")`：厂商日频技术指标，支持通达信 74 分量、Tushare 226 固定字段、iFinD 28 原生日线字段与 Wind 32 历史字段、21 最新快照字段，按字典选择来源、日期能力、复权和参数。
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

`dataset="technical_indicators"` 使用厂商数值，不自行重算。先用 `search_metrics` 搜索指标；需要选择族、分量、参数时读取 `data_catalog(dataset="technical_indicators")` 的 `profiles`。共 381 个字段，其中原有历史目录 300 项、iFinD 独立日线 28 项、Wind 历史 32 项、独立快照 21 项：

- 通达信 29 族、74 分量：KDJ、MACD、RSI、BOLL、MA、EXPMA、BBI、DMI、CCI、ROC、MTM、WR、BIAS、TRIX、PSY、OBV、VR、VOL、ATR、DMA、EMV、DPO、BRAR、MFI、CR、ASI、MASS、XSII、唐奇安通道。
- Tushare 专业版 78 个输出分量／固定周期项、226 字段：ASI/ASIT、ATR、BBI、BIAS、BOLL、BRAR、CCI、CR、DFMA、DMI、DPO/MADPO、EMA、EMV/MAEMV、EXPMA、KDJ、KTN、MA、MACD、MASS/MA_MASS、MFI、MTM/MTMMA、OBV、PSY/PSYMA、ROC/MAROC、RSI、TAQ、TRIX/TRMA、VR、WR/WR1、XSII，以及上涨／下跌／高点／低点天数。MA/EMA 有 5/10/20/30/60/90/250 日固定项，RSI 有 6/12/24 日固定项。分量和周期项不等于独立算法数量。

iFinD 使用独立 `ifind_*` 字段，例如 `ifind_ma`、`ifind_kdj_k`；仅不复权日线，`adjust="native"` 沿用不复权定义，也接受 `none`。参数以各 profile 为准，返回必须逐分量提供匹配参数与显式表格证券、日期；省略指标或参数不符不能作为成功数据。它不会填入通达信或 Tushare 的字段，也不承诺跨来源算法相等。

通达信普通查询省略 `parameters` 使用字段保存参数，显式参数局部覆盖。整数上下限以 profile 为准；MA 部分均线参数允许 0，表示关闭，对应值为空。跨 profile 查询省略参数；显式参数要求字段属于同一个可调 profile。厂商 `params_used` 必须与请求一致；BOLL 只有已适配的 `m`，无自定倍数。

Tushare 字段为来源预计算固定参数，`fixed=true`，不支持 `parameters` 覆盖。直接选择所需周期与复权字段，例如 `ts_ma_qfq_20` 是前复权 MA20；不存在的任意周期不能靠改参数得到。各字段的 `fixed_parameters` 保留原文说明，BBI 的不同复权字段参数说明存在冲突，不替来源统一。

仅支持 `frequency="daily"`。`adjust="native"` 表示沿用字段定义：Tushare 的 `qfq` / `hfq` / `bfq` 字段分别为前复权／后复权／不复权；通达信未披露复权方式，不能称为不复权。Tushare 也可传与全部所选字段一致的 `qfq` / `hfq` / `none`；跨口径查询用 `native` 保留各字段定义。回答注明字段、实际参数或固定参数、复权、日期与实际来源。

按字段的 `unit_status` / `unit_note` 解释单位，不将按定义推断说成厂商明确承诺。通达信 VOL、OBV 为手，日线量价 `volume` 为股；1 手为 100 股。OBV 起点由厂商决定。ATR 单位为元；通达信 WR 是 0–100 标度，越小越接近区间高点，不改成负值 Williams %R。VR 的原生数值不能当收益率；通达信以 % 标记比率乘 100 的标度，Tushare 标无量纲，跨来源不据此互换。ASI、EMV 等未知单位保留原生数值及未确认状态，不凭名称换算或补单位。

通达信探索可用明确分量名（如 `MACD DIF`）及参数，成功后按已有保存授权保存一个分量。单位未披露的通达信分量暂不能保存为单位已确认定义。Tushare 固定分量已在字典中，探索命中时复用普通查询，返回 `existing`，无需重复保存。保存的通达信定义再次查询可省略参数复用，或局部覆盖。

Wind 历史查询使用 `wind_history_*`，共23类、32字段。例：`fields=["wind_history_macd_dif","wind_history_macd_dea","wind_history_macd_macd"]`，或 `fields=["wind_history_ma"], parameters={"n":10}`；均需起止日期。MA默认20、RSI默认6，周期可调并核对返回参数；其他族沿用来源默认，不声称MACD必为12/26/9。布林线当前固定26日、带宽2。只支持 `adjust="native"`；未披露复权时不能称为不复权。BR缺项；VR/OBV保留未确认缩放的来源原值。字典已定义字段的探索直接复用查询，不再猜问句。

Wind 的 `wind_*_native` 字段是最新盘中快照，不补齐其他来源的历史字段。读取实际交易日和交易时间；请求区间不含实际交易日时应失败，不把请求日期写到快照上。Wind 完整参数和复权方式未披露，不允许覆盖参数，只使用 `adjust="native"`。AKShare 当前已核实官方目录与安装版本没有可接入本目录的原生历史指标数值接口，保留缺项；不得把选股榜单当作历史技术值，也不得把本地计算归为 AKShare 原生返回。

不支持任意公式、任意 Tushare 周期参数、周线／分钟线或目录外未映射指标。通达信 EXPMA 的 EMA 别名不是独立 EMA 接口；唐奇安仅上下轨，不冒称 TAQ 三轨，TAQ/KTN 三轨由 Tushare 固定字段提供。通达信最近日线声明上限 5000 根不代表已验证能取满；Tushare 每页上限 10000 行，最多 20 页。样本验证不证明所有股票、日期或跨来源算法一致。

## 回答与结果

先给用户所问的数据或结论，注明证券、实际日期／报告期、单位与来源。逐值来源以 `field_sources` 为准；同表可能混合多个来源。缺值和未确认单位如实保留，不自行补数、换算或替换口径。财务报告期数据不能据此声称是某历史时点已可获取的数据。

查询只返回前 100 行预览及完整本地结果路径；全量分析读取完整结果，不把预览当全集。需要交付完整数据时引用返回的文件或下载地址，不把大量原始表格塞入上下文。不要读取、回传或修改凭证配置，也不要借保存指标修改路由、来源开关或账户权限。
