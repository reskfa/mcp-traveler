# 羊毛伞 · A 股与可转债数据网关

> **一个 MCP 接入六家数据源。** Agent 只需连一个端点，就能查 A 股和可转债的量价、估值、财务和技术指标——Tushare、AKShare、Wind、iFinD、通达信、东方财富妙想，缺字段自动向后补齐，每个值标明来源。

```
┌─────────────┐    ┌────────────────────┐    ┌──────────────────┐
│  你的 Agent  │───▶│  羊毛伞 (本机网关)  │───▶│  Tushare         │
│  本地网页    │    │                    │    │  AKShare         │
│  Python/HTTP │    │  统一查询核心       │    │  Wind MCP        │
│              │    │  路由 → 补齐 → 标注 │    │  iFinD MCP       │
└─────────────┘    └────────────────────┘    │  通达信 MCP      │
                                             │  东方财富妙想 MCP │
                                             └──────────────────┘
```

## ✨ 核心价值

| 能力 | 说明 |
|------|------|
| **统一接口** | 网页、Python、HTTP、MCP 共用一个查询核心，Agent 只接一个端点 |
| **六家来源** | 按你的账户权限选择，不需要全部开通 |
| **逐字段补齐** | 优先来源缺的字段自动从下一家补，已有值不动，每个值标明来源 |
| **574 个内置字段** | 量价、估值、财务、技术指标已预置取法、单位转换和跨源映射 |
| **路由可调** | 网页拖拽排序来源优先级，下次查询生效 |
| **探索 + 个人字典** | 字典没有的指标让 Agent 探索，成功后保存取法，之后像内置字段一样复用 |
| **密钥本机管理** | 所有密钥、配置、结果存在你的电脑，不经第三方 |

## 🚀 60 秒启动

需要 Python 3.11+。

```bash
git clone https://github.com/reskfa/mcp-traveler.git
cd mcp-traveler
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m gateway
```

> Windows 将 `python3` → `python`，`.venv/bin/python` → `.venv\Scripts\python.exe`。

打开 http://127.0.0.1:8765 → 在"数据源"页填写你有的密钥 → 完成。

MCP 端点：`http://127.0.0.1:8765/mcp`。端口冲突加 `--port 8766`。

## 🔌 接入 Agent

在支持 Streamable HTTP 的 MCP 客户端中加一行：

```json
{
  "mcpServers": {
    "ashare-gateway": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

然后直接对 Agent 说：

> 用 ashare-gateway 查贵州茅台 2025-11-03 到 2025-11-05 的不复权日线，再查 2024 年年报营业收入和归母净利润，注明来源。

**配套 Skill**：将 `skills/ashare-gateway/` 复制到客户端的 skills 目录，Agent 就知道怎么搜索、探索、保存和复用字段。详见 [SKILL.md](skills/ashare-gateway/SKILL.md)。

## 🛠 三分钟试用

1. **查日线**：默认 `600519.SH`，查 `2025-11-03` ~ `2025-11-05`，看看 Tushare 返回。
2. **切来源**：换 Wind / AKShare / 通达信，对比数据和来源标注。
3. **查财务**：报告期 `2024-12-31`，选营收和归母净利润。
4. **调路由**：上下箭头排序来源优先级并保存，下次"自动"查询生效。
5. **补齐效果**：选"自动"，第一来源缺的字段会从后续来源补入，每个值下方显示来源。

## 📡 MCP 工具速查

**单券查询**

| 工具 | 用途 |
|------|------|
| `query_daily_prices` | 日线量价 |
| `query_valuations` | 估值与每日指标 |
| `query_financials` | 基础财务 |
| `query_technical_indicators` | 技术指标 |
| `query_basic_info` | 公司与证券档案 |
| `data_catalog` | 查字典定义 |
| `search_metrics` | 按中文名/代码搜索字段 |

**跨券与全市场**（优先用这三个，不要让 Agent 循环单券接口）

| 工具 | 用途 |
|------|------|
| `query_market_universe` | A 股/可转债名单（含历史退市） |
| `query_market_daily` | 多券或全市场日线 |
| `query_market_returns` | 区间收益排名（如"近 5 日跌幅最大转债"） |

省略 `symbols` 才是全市场，空数组会被拒绝。三个市场工具默认返回完整 JSON；`top_n` 只在完整取数、计算和排序后裁剪响应，完整结果仍保存在本地。近 5 日排名使用 `trading_days=5`、`order="asc"`，窗口包含结束日并另取前一交易日为基准。缺价格、停牌无行或混合来源不当作 0；`coverage.ranking_complete=false` 时只能称为合格输入中的 Top N。非交易日明确报错，不静默改日期。

**探索与扩展**

| 工具 | 用途 |
|------|------|
| `explore_metric` | 字典没有的指标，让网关尝试取数 |
| `save_metric` | 探索成功后保存取法到个人字典 |
| `explore_data` / `query_data` | 通用取数探索与复用 |
| `search_capabilities` | 搜索供应商工具目录 |

## 🐍 Python / HTTP

```python
import asyncio
from gateway.core import Query, query

result = asyncio.run(query(Query(
    dataset="daily_prices", symbol="600519.SH",
    start_date="2025-11-03", end_date="2025-11-05",
    fields=["close", "volume"], provider="auto"
)))
print(result["data"])
```

HTTP：`POST /api/query`（单券）、`POST /api/market/universe|daily|returns`（跨券）。日期 `YYYY-MM-DD`，代码带 `.SH/.SZ/.BJ`。

---

## 📚 详细文档

以下内容适合需要深入了解的用户，日常使用看上面就够了。

<details>
<summary><strong>来源能力与限制</strong></summary>

| 来源 | 已验证 | 限制 |
| --- | --- | --- |
| Tushare | 日线、9 项基础财务；探索期验证历史估值 | 复权采用当前完整因子历史的首／末日基准；财务不是历史时点数据库 |
| AKShare | 东财失败后新浪备用日线、新浪财务及扣非／ROE | 东财可能拒绝连接；百度 PE/PB 为探索接口，市值单位未确认，暂留空 |
| Wind | 固定参数日线、历史估值、基础财务 | 自然语言财务可能以亿元显示；扣非单独调用，PE 标题有时省略 TTM，结果会提示 |
| iFinD | 历史 PE/PB、市值、基础财务 | ROE 使用加权公布值，可能不同于其他来源的平均净资产口径 |
| 通达信 | 固定参数历史日线、三大报表、历史估值 | ROE、K 线换手率暂缺；自动查询可由其他来源补齐 |
| 东方财富妙想 | 自然语言数据浏览器：日线、估值、财务、基本信息、可转债简称 | 返回为指标×日期转置表；技术指标与公告事件未预置，保留 explore_data / explore_metric |

日期、字段、单位尽量统一，但不承诺所有来源同口径。部分字段留空并标明；无法确认日期的历史请求不会用最新快照替代。复权价格不跨源拼接。

</details>

<details>
<summary><strong>逐字段补齐机制</strong></summary>

自动路由遇到缺项时，只向后续来源请求缺失字段，按证券及相同日期／报告期补齐，已有数值保持不变。结果中的 `field_sources` 与 `data` 逐行对应，网页在每个数值下显示来源；原始返回保留各来源及请求。最新估值补齐时锁定首个结果的交易日。复权价格不跨来源拼接。首个正常空结果仍直接返回；补齐过程中遇到空结果、错误或超时，会继续往后，全部来源用完后仍保留已有结果。指定来源时不补齐。默认每个来源最多尝试一次；AKShare 日线内部在东财失败后可改用独立的新浪上游，结果会标明。

</details>

<details>
<summary><strong>配置、账户和数据</strong></summary>

- `.env`：真实密钥。`config.json`：来源开关、地址、鉴权、超时及路由。两者只保存在本机，已忽略于 Git。
- `market`：跨券批量、并发、请求间隔、总时限、最大代码数和证券×交易日上限；旧配置自动使用默认值，无需迁移。
- `local/results/`：原始结果和完整标准表；每次查询独立保存，无自动清理。
- `local/account_status.json`：连接状态、本地查询次数、手动额度记录。次数是网关查询次数，一次查询可能调用多个上游工具，不能代表扣费次数。
- **官方余额自动刷新尚未实现**。刷新入口会提示登录官网核实，允许手动记录并标明时间。
- 服务仅监听本机，检查 Host、来源和连接地址；配置读取不返回完整密钥。不要将服务直接暴露到公网。

官方入口：[Wind](https://market.windalice.com/)、[iFinD](https://mcp.51ifind.com/)、[通达信](https://vip.tdx.com.cn/)、[东方财富妙想](https://mxapi.eastmoney.com/mxds/doc/mcp-install.md)、[Tushare](https://tushare.pro/)。

</details>

<details>
<summary><strong>项目级数据字典</strong></summary>

[总字典表](docs/data-dictionary.md) 列出全部 574 个内置分类字段（量价 7、估值与每日指标 17、财务 169、技术指标 381）在六家来源的取法、返回列、单位转换及缺项。

内置字典是 `gateway/data/dictionary.json`，人读的 Markdown 由它生成；个人扩展保存在 `local/user_dictionary.json`。运行时合并两者，取数执行器、数据目录和字段解析共用合并后的字典。

- `datasets.*.fields`：统一字段含义、单位、别名及每家来源的绑定。
- `recipes`：SDK 方法或 MCP 工具、参数模板、分页、备用接口及问句模板。
- `gateway/dictionary.py`：校验字典并按请求字段生成调用计划。
- `gateway/acquisition.py`：执行计划、标准化数据、合并同一来源的多个接口返回。
- `gateway/providers.py`：凭证、MCP 传输与 SDK 初始化，不再维护业务字段列表。

字段绑定指定 `recipe`、`columns`、可选 `multiplier`、`prompt` 等。一个接口的多个字段合并调用；独立接口分别调用。缺少取法的字段不发出猜测请求。新增已有接口的字段通常只需编辑字典；接入新的返回结构或协议仍需扩展执行器。

字典修改会在下一次查询或搜索时加载，无需重启。修改内置字典后同步文档：

```bash
python scripts/render_dictionary.py
python scripts/render_dictionary.py --check
```

完整机器字典：`GET /api/dictionary`。单字段查询示例：`GET /api/dictionary?dataset=financials&field=net_profit_excl`；MCP 使用 `data_catalog(dataset="financials", field="net_profit_excl")`。

</details>

<details>
<summary><strong>Tushare 字段扩展</strong></summary>

估值与每日指标覆盖 [daily_basic](https://tushare.pro/document/2?doc_id=32) 的全部 17 个业务字段，包括股息率、股本和非默认返回的涨跌停状态。财务覆盖 [fina_indicator](https://tushare.pro/document/2?doc_id=79) 的全部 163 个业务字段，保留原有报表字段后共 169 项。逐项对应见 [覆盖清单](docs/tushare-coverage.json)。

默认仍查询原有常用字段（量价 7、估值 4、财务 9）；API / MCP 传入 `fields=["*"]` 可查询该类别全部字段，或指定如 `fields=["dt_eps", "q_netprofit_yoy"]`。网页可按中文名称或代码搜索，再选择字段。其他来源的对应关系与留空原因见[跨来源核对记录](docs/cross-source-review.md)。

</details>

<details>
<summary><strong>厂商技术指标</strong></summary>

类别 `technical_indicators` 共 **381 个字段**：原有 300 个历史字段，加 iFinD 28 个独立原生日线字段和 Wind 32 个历史字段、21 个独立最新快照字段。原有历史字段包括通达信 29 个指标族、74 个分量；Tushare 技术面因子专业版 78 个输出分量／固定周期项、226 个复权字段。

| 来源 | 已支持范围 | 参数与复权 |
| --- | --- | --- |
| iFinD | 28 个已验证分量，使用独立 `ifind_*` 字段 | 不复权日线；按 profile 调参 |
| Wind | 23 类、32 个 `wind_history_*` 历史字段，以及 21 个 `wind_*_native` 快照字段 | 历史 MA/RSI 周期可调，布林线26日/带宽2 |
| 通达信 | KDJ、MACD、RSI、BOLL、MA 等 29 族 | 按各 profile 调参；日线原生口径 |
| Tushare 专业版 | ASI、ATR、BBI、BIAS、BOLL、BRAR、CCI、CR 等 30 类，含不复权/前复权/后复权 | 来源预计算固定参数 |

查询示例：

```text
query_technical_indicators(symbol="600519.SH", start_date="2026-11-10", end_date="2026-11-13",
  fields=["wind_history_macd_dif", "wind_history_macd_dea"], provider="wind")
query_technical_indicators(symbol="600519.SH", start_date="2026-11-12", end_date="2026-11-13",
  fields=["macd_dif", "macd_dea", "macd_macd"], provider="tdx",
  parameters={"short":12,"long":26,"mid":9})
```

Tushare 全部 226 个源字段及逐项映射见[技术目录边界](docs/technical-inventory.md)和[覆盖清单](docs/tushare-technical-coverage.json)。Wind/iFinD/AKShare 见[其他来源技术指标](docs/technical-other-sources.md)。

</details>

<details>
<summary><strong>搜索、探索与个人字典</strong></summary>

网页进入"取数与指标"，可在"手动试查"和"指标字典"之间切换。按中文名、字段 ID 或别名搜索，已有指标直接"选到试查"。

Agent 探索成功后展示数值、单位、实际来源和尝试记录；取法明确时默认保存，也可通过 `save_metric` 手动保存并自定义英文 ID。之后通过普通查询这个 ID，执行已保存的取法，不再重新探索。

MCP 工具：
- `search_metrics(query, dataset?, limit=10)`：搜索匹配的指标。
- `explore_metric(name, dataset, symbol, ...)`：未知指标按路由尝试。
- `save_metric(exploration_id, field?)`：将成功候选写入个人字典。

HTTP 对应 `GET /api/metrics/search?q=净息差`、`POST /api/explore`、`POST /api/explorations/{id}/save`、`DELETE /api/metrics/{dataset}/{field}`。

**探索边界**：只探索 A 股数值指标；默认最多尝试两个已配置来源。可保存候选必须匹配证券、实际日期和指标名称。保存的是可复用的字段绑定，不是当次答案。个人字典 `local/user_dictionary.json` 被 Git 忽略，更新代码不覆盖。

</details>

<details>
<summary><strong>股票基本信息</strong></summary>

56 个字典字段，分为公司与证券档案、证券名称历史、行业归属、板块与标签四组。网页选择"股票基本信息"，或使用 MCP `query_basic_info(symbol="300308.SZ", fields=["company_name", "ownership_type", "name_history", "ths_concepts"])`。详见 [基本信息字段、取法与验证](docs/basic-info.md)。

</details>

<details>
<summary><strong>可转债</strong></summary>

可转债沿用"搜索字典 → 探索 → 保存 → 复用"流程，独立于股票接口。用 `search_metrics(dataset="cb_basic" / "cb_daily" / "cb_clauses")` 查定义，再调用 `query_convertible_bond_basic`、`query_convertible_bond_daily` 或 `query_convertible_bond_clauses`。

三来源已实测：AKShare 当前档案、历史 OHLC 与估值；iFinD 当前档案及历史债券指标；TDX 固定历史日K与简称。详见[逐来源能力与证据](docs/convertible-bonds-expansion.md)。

</details>

<details>
<summary><strong>通用数据探索（explore_data / query_data）</strong></summary>

字典未命中时，先在网关内探索，不直接转外部搜索。

- `search_capabilities(query, provider="auto", query_scope="security", market_hint?, market_kind?)`：匹配缓存的供应商工具目录。
- `explore_data(request, symbol?, ...)`：在网关内尝试取数，成功后保存可复用取法。全市场探索省略 `symbol`，传 `query_scope="market"`、`market_hint="equity|convertible_bond"`、`market_kind="universe|daily"`；名单历史时点用 `end_date`。
- `query_data(query_id, symbol?, ...)`：复用已保存取法；旧单券取法仍传代码，市场取法省略代码并可换日期。

市场探索只接受 `universe` 和 `daily`，逐行验证真实证券代码、市场类型、实际日期和所请求字段，按 `(symbol,date)` 检查重复与冲突。响应不裁 100 行，但只有上游总数一致且无节选证据时才标记本次完整；保存取法不等于已经取全，保存内容不含代码清单。

</details>

## 验证

```bash
.venv/bin/python -m pytest -q
```

离线测试覆盖逐字段补齐、已有值及零值保留、日期对齐、换源、配置持久化、密钥遮罩、本机访问限制等。真实接口测试会消耗厂商额度，不包含在默认测试中。

## 许可证

代码使用 [MIT License](LICENSE)。各数据源的账户、数据使用及再分发权限仍由相应厂商条款决定。本项目与上述数据厂商无隶属关系。

---

**这是可试用的早期 MVP。** 欢迎提交 [Issue](https://github.com/reskfa/mcp-traveler/issues)，附上来源、查询参数和脱敏后的报错；不要上传密钥或账户信息。
