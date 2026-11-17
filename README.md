# 渡数 · A 股数据网关

本地运行的 A 股取数 MVP：Tushare、AKShare、Wind MCP、iFinD MCP、通达信 MCP，共用一个 Python 查询核心，提供本地网页和统一 MCP。

Agent 只需接入一个 MCP，就能查询 A 股量价、估值和基础财务。通过网页设置来源顺序，缺失字段自动向后补齐，每个数值保留来源。

- **统一入口**：本地网页、Python、HTTP 和 MCP 共用查询核心。
- **五家来源**：按自己的账户权限选择，不需要全部开通。
- **逐字段补齐**：保留优先来源已有值，后续只补空缺。
- **本机配置**：密钥、账户记录和查询结果保存在本地。

这是可试用的早期 MVP。欢迎提交 Issue，附上来源、查询参数和脱敏后的报错；不要上传密钥或账户信息。

## 立即体验

需要 Python 3.11+。macOS / Linux：

```bash
git clone https://github.com/reskfa/mcp-traveler.git
cd mcp-traveler
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m gateway
```

Windows PowerShell 中将 `python3` 换为 `python`，将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。

网页：<http://127.0.0.1:8765>，MCP：<http://127.0.0.1:8765/mcp>。端口被占用时可使用 `--port 8766`。终端按 Ctrl+C 停止服务。

第一次启动若没有配置，会从 `.env.example`、`config.example.json` 复制模板，不覆盖已有配置。在网页“数据源”里填写密钥和 MCP 地址即可。

## 三分钟试用

1. **试查数据**：保留 `600519.SH`，查 `2025-11-03` 至 `2025-11-05` 日线；默认先用 Tushare。
2. **切换来源**：分别选择 Wind、AKShare 或通达信，比较数据和来源说明。金额统一元、成交量统一股；原始返回和完整 CSV / JSON 可下载。
3. **基础财务**：报告期选 `2024-12-31`，建议先选营收和归母净利润。Tushare 默认字段覆盖最全。
4. **路由顺序**：用上下箭头调整来源并保存。下一次选择“自动”时生效；指定来源不换源。
5. **数据源**：测试连接，修改密钥（留空保留旧值），或手动记录官方积分和剩余次数。

自动路由遇到缺项时，只向后续来源请求缺失字段，按证券及相同日期／报告期补齐，已有数值保持不变。结果中的 `field_sources` 与 `data` 逐行对应，网页在每个数值下显示来源；原始返回保留各来源及请求。最新估值补齐时锁定首个结果的交易日。复权价格不跨来源拼接。首个正常空结果仍直接返回；补齐过程中遇到空结果、错误或超时，会继续往后，全部来源用完后仍保留已有结果。指定来源时不补齐。默认每个来源最多尝试一次；AKShare 日线内部在东财失败后可改用独立的新浪上游，结果会标明。

## 接入 Agent

在支持 Streamable HTTP 的 MCP 客户端中添加以下配置。无需把厂商密钥再次提供给 Agent：

```json
{
  "mcpServers": {
    "ashare-gateway": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

这是通用 Streamable HTTP 配置示例；具体客户端的配置位置、传输类型选项可能不同，按客户端要求填写。远端云端 Agent 无法直接访问你电脑的 localhost，本版面向本机 Agent。

常规查询工具：`data_catalog`、`query_daily_prices`、`query_valuations`、`query_financials`、`query_technical_indicators`、`provider_status`。

可直接提问：

> 用 ashare-gateway 查贵州茅台 2025-11-03 到 2025-11-05 的不复权日线，再查 2024 年年报营业收入和归母净利润，注明来源。

查询只返回前 100 行预览及完整结果路径，避免把大量数据放入上下文。Agent 先用 `search_metrics` 搜索需要的指标，已有字段直接查询；需要详细口径时再用 `data_catalog(dataset, field)` 查单字段，不必加载整本字典。未知指标可用 `explore_metric` 探索，有成功证据且用户已授权加入常用字典时，用 `save_metric` 保存，之后普通查询复用取法，无需每次重新探索或重复请求保存审批。

### 配套 Skill

[ashare-gateway Skill](skills/ashare-gateway/SKILL.md) 教 Agent 搜索、查询、探索及复用个人字典，并保留逐值来源、单位和日期口径。将整个 `skills/ashare-gateway` 文件夹复制到客户端支持的 skills 目录；例如支持该约定的客户端可放到 `~/.agents/skills/ashare-gateway/`，Codex 也可放到 `~/.codex/skills/ashare-gateway/`。以客户端自己的发现规则为准。

Skill 是使用说明，安装它不会自动配置 MCP。先完成上面的 MCP 连接，再让 Agent 使用 `ashare-gateway` Skill；厂商密钥继续由本机网关管理。

## Python / HTTP

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

HTTP 使用 `POST /api/query`，请求结构同 `Query`。本地网页与 MCP 共用同一个核心。请求日期均为 `YYYY-MM-DD`，代码需带 `.SH` / `.SZ` / `.BJ`。

## 配置、账户和数据

- `.env`：真实密钥。`config.json`：来源开关、地址、鉴权、超时及路由。两者只保存在本机，已忽略于 Git。
- `local/results/`：原始结果和完整标准表；每次查询独立保存，无自动清理。连接测试保存工具定义快照，日常查询不重复枚举工具。
- `local/account_status.json`：连接状态、本地查询次数、手动额度记录。次数是网关查询次数，一次查询可能调用多个上游工具，不能代表扣费次数。
- **官方余额自动刷新尚未实现**。刷新入口会提示登录官网核实，允许手动记录并标明时间。本地统计不冒充官方额度。
- 服务仅监听本机，检查 Host、来源和连接地址；配置读取不返回完整密钥。不要将服务直接暴露到公网。
- 编辑样例文件不会修改本机配置；不要把真实凭证放进 `.env.example` 或公开仓库。

官方入口：[Wind](https://market.windalice.com/)、[iFinD](https://mcp.51ifind.com/)、[通达信](https://vip.tdx.com.cn/)、[Tushare](https://tushare.pro/)。

## 首版能力与限制

| 来源 | 已验证 | 限制 |
| --- | --- | --- |
| Tushare | 日线、9 项基础财务；探索期验证历史估值 | 复权采用当前完整因子历史的首／末日基准；财务不是历史时点数据库 |
| AKShare | 东财失败后新浪备用日线、新浪财务及扣非／ROE | 东财可能拒绝连接；百度 PE/PB 为探索接口，市值单位未确认，暂留空 |
| Wind | 固定参数日线、历史估值、基础财务 | 自然语言财务可能以亿元显示；扣非单独调用，PE 标题有时省略 TTM，结果会提示 |
| iFinD | 历史 PE/PB、市值、基础财务 | ROE 使用加权公布值，可能不同于其他来源的平均净资产口径 |
| 通达信 | 固定参数历史日线、三大报表、历史估值 | ROE、K 线换手率暂缺；自动查询可由其他来源补齐 |

日期、字段、单位尽量统一，但不承诺所有来源同口径。部分字段留空并标明；无法确认日期的历史请求不会用最新快照替代。复权价格不跨源拼接；没有全市场批量建库和实时推送。

## 验证

```bash
.venv/bin/python -m pytest -q
```

离线测试覆盖逐字段补齐、已有值及零值保留、日期对齐、补齐失败保留部分结果、独立指定来源、Wind 扣非分块合并、数值单位、三家 MCP 返回结构、通达信会话流程、配置持久化、密钥遮罩、本机访问限制、模拟失败换源、正常空结果和下载。真实接口测试另行执行，会消耗厂商额度，不包含在默认测试中。


## 许可证

代码使用 [MIT License](LICENSE)。各数据源的账户、数据使用及再分发权限仍由相应厂商条款决定，MIT 不授予上游数据的使用权。本项目与上述数据厂商无隶属关系。


## 项目级数据字典

[总字典表](docs/data-dictionary.md) 列出全部 574 个内置分类字段（量价 7、估值与每日指标 17、财务 169、技术指标 381）在五家来源的取法、返回列、单位转换及缺项。

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

完整机器字典：`GET /api/dictionary`。单字段查询示例：`GET /api/dictionary?dataset=financials&field=net_profit_excl`；MCP 使用 `data_catalog(dataset="financials", field="net_profit_excl")`。字典不包含密钥；用户路由仍独立保存在本机 `config.json`。

### Tushare 字段扩展

估值与每日指标覆盖 [daily_basic](https://tushare.pro/document/2?doc_id=32) 的全部 17 个业务字段，包括股息率、股本和非默认返回的涨跌停状态。财务覆盖 [fina_indicator](https://tushare.pro/document/2?doc_id=79) 的全部 163 个业务字段，保留原有报表字段后共 169 项。证券代码、公告日期、报告期及更新标记不计入指标数；逐项对应见 [覆盖清单](docs/tushare-coverage.json)。

默认仍查询原有常用字段（量价 7、估值 4、财务 9）；API / MCP 传入 `fields=["*"]` 可查询该类别全部字段，或指定如 `fields=["dt_eps", "q_netprofit_yoy"]`。网页可按中文名称或代码搜索，再选择字段；全选会包含搜索结果之外的字段。其他来源的对应关系与留空原因见[跨来源核对记录](docs/cross-source-review.md)。

财务指标包含期末数、累计数、单季度数及增长率，具体口径见字典。官方文档未说明单位的新增指标标为“未确认”，保留原始数值，不推断比例和百分数的换算。非金融企业与银行的适用字段不同，空值原样保留。

跨源映射统一业务含义与单位，保留供应商自己的计算算法及数值；Tushare仅作为字段目录参照。查询结果通过`field_sources`标明逐值来源，`fields.<字段>.source_definitions`说明来源口径。iFinD历史配对已验证表格日期和值随查询改变，参数说明中的“最新/MRQ”不再单独作为拒绝理由；没有实际日期证据或返回错误日期仍不接受。详见[跨来源核对记录](docs/cross-source-review.md)。


## 搜索、探索与个人字典

网页进入 **指标字典**，按中文名、字段 ID 或别名搜索。已有指标可以直接“选到试查”；找不到时展开“探索未知指标”，填写证券、日期或报告期、指标名，可选填单位、别名和指定来源。

探索成功后先展示数值、单位、实际来源和尝试记录，再点击保存，写入自己的常用字典。可以自定义英文 ID，例如 `net_interest_margin`。之后通过普通试查或财务 MCP 查询这个 ID，执行已保存的取法，不再重新探索。删除个人指标只移除本地扩展。

新增 MCP 工具：

- `search_metrics(query, dataset?, limit=10)`：只返回匹配的简短指标资料，建议 Agent 先搜索，不必每次读取整份字典。
- `explore_metric(name, dataset, symbol, period?/date?/start_date?/end_date?, unit?, aliases?, provider="auto", max_attempts=2)`：已知名称直接用字典；未知指标按路由尝试，返回候选及证据。
- `save_metric(exploration_id, field?)`：将成功候选写入个人字典。保存后普通查询和搜索立即可用。

例如先搜索“净息差”，没有时探索 `financials / 000001.SZ / 2024-12-31`，再保存为 `net_interest_margin`；之后可调用 `query_financials(symbol="000001.SZ", period="2023-12-31", fields=["net_interest_margin"])`。

HTTP 对应 `GET /api/metrics/search?q=净息差`、`POST /api/explore`、`POST /api/explorations/{id}/save`（请求体可含 `field`）和 `DELETE /api/metrics/{dataset}/{field}`。探索请求的其他参数与普通查询相同，以 `name` 指定指标。

### 当前探索边界

只探索 A 股数值指标；一般指标使用已接入的 Wind、iFinD、通达信自然语言工具，技术指标使用已适配的厂商接口（见下节）。Tushare、AKShare 继续走明确的字典映射，不猜 SDK 方法。默认最多尝试两个已配置来源，可设为 1–3；每来源最多一次、最长 60 秒。尝试次数不等于厂商扣费次数。

候选必须匹配证券、实际日期、指标名称或用户提供的别名，并能确认数值及单位；明确要求单季度、累计、期末或 TTM 时还会检查对应说明。缺证据时展示待确认原因，不自动保存。来源计算方法允许不同，保留来源和原生口径说明。一次样例成功并不代表该来源覆盖所有股票、年份；后续查询仍可能为空。

保存的是可复用的字段绑定与参数模板，不是当次答案。当前每次保存一个新指标及一个成功来源，已有 ID 不覆盖；其他来源的对应取法可后续补入个人字典。个人字典使用与内置字典相同的字段结构；自定义执行配方仅允许复用已支持的取法。新的 SDK 接口或全新返回结构仍需要开发适配。

`local/user_dictionary.json` 保存常用指标，`local/explorations/` 保存探索证据，均被 Git 忽略。更新代码不覆盖个人字典；迁移电脑时请自行备份该文件。

本轮真实验证：Wind 返回平安银行 2024 年净息差 1.87%，网页保存后，从 MCP 普通财务查询取得 2023 年 2.38%。该验证只证明这个指标在这两个报告期的流程，不代表其他指标已验证。

## 厂商技术指标

类别 `technical_indicators` 共 **381 个字段**：原有 300 个历史字段，加 iFinD 28 个独立原生日线字段和 Wind 32 个历史字段、21 个独立最新快照字段。原有历史字段包括通达信 29 个指标族、74 个分量；Tushare 技术面因子专业版 78 个输出分量／固定周期项、226 个复权字段。数值来自厂商，不在本地重算。来源、分量、单位和参数由 `data_catalog(dataset="technical_indicators")` 的 `profiles` 与字段定义统一描述，网页据此生成选项。

| 来源 | 已支持范围 | 参数与复权 |
| --- | --- | --- |
| iFinD | 28 个已验证分量，使用独立 `ifind_*` 字段 | 不复权日线；按 profile 调参，逐分量核对返回参数；省略分量或参数不符即失败 |
| Wind | 23 类、32 个 `wind_history_*` 历史字段，以及 21 个 `wind_*_native` 快照字段 | 历史 MA/RSI 周期可调，其余沿用来源默认；布林线26日、带宽2；快照不代替历史 |
| AKShare | 当前已核实范围没有本目录可接入的原生历史数值接口 | 技术选股榜单不充当历史数值；未启用本地派生 |
| 通达信 | KDJ、MACD、RSI、BOLL、MA、EXPMA、BBI、DMI、CCI、ROC、MTM、WR、BIAS、TRIX、PSY、OBV、VR、VOL、ATR、DMA、EMV、DPO、BRAR、MFI、CR、ASI、MASS、XSII、唐奇安通道 | 按各 profile 调参；日线原生口径，复权方式未披露 |
| Tushare 专业版 | ASI/ASIT、ATR、BBI、BIAS、BOLL、BRAR、CCI、CR、DFMA、DMI、DPO/MADPO、EMA、EMV/MAEMV、EXPMA、KDJ、KTN、MA、MACD、MASS/MA_MASS、MFI、MTM/MTMMA、OBV、PSY/PSYMA、ROC/MAROC、RSI、TAQ、TRIX/TRMA、VR、WR/WR1、XSII，以及上涨／下跌／高点／低点天数 | 来源预计算固定参数；分别选不复权、前复权、后复权字段，天数项单独定义 |

Wind 历史覆盖 MA、MACD、KDJ、RSI、BOLL、CCI、ATR、BIAS、ROC、WR、OBV、EXPMA、BBI、DMI、TRIX、PSY、MTM、DMA、SAR、MFI、AR、VR、CR。历史查询示例：

```text
query_technical_indicators(symbol="600519.SH", start_date="2026-11-10", end_date="2026-11-13",
  fields=["wind_history_macd_dif", "wind_history_macd_dea", "wind_history_macd_macd"], provider="wind")
query_technical_indicators(symbol="600519.SH", start_date="2026-11-10", end_date="2026-11-13",
  fields=["wind_history_ma"], parameters={"n":10}, provider="wind")
```

使用字典中的简短问句和精确返回列映射；一个指标族一次调用，选择单个分量也可复用整族返回。证券、实际日期、已声明分量和参数必须匹配。BR 尚无可信取法；VR、OBV 未明确缩放，保留来源原值。未披露的周期和复权方式不猜测，也不声称与其他来源算法相同。

Tushare 全部 226 个源字段及逐项映射见[技术目录边界](docs/technical-inventory.md)和[覆盖清单](docs/tushare-technical-coverage.json)。其中 MA、EMA 提供 5/10/20/30/60/90/250 日固定项，RSI 提供 6/12/24 日固定项；“78 项”包含同一算法的分量和周期项，不表示 78 种独立算法。

```text
query_technical_indicators(
  symbol="600519.SH", start_date="2026-11-12", end_date="2026-11-13",
  fields=["macd_dif", "macd_dea", "macd_macd"], provider="tdx",
  parameters={"short":12,"long":26,"mid":9}, frequency="daily", adjust="native"
)
query_technical_indicators(
  symbol="600519.SH", start_date="2026-11-12", end_date="2026-11-13",
  fields=["ts_ma_qfq_20", "ts_atr_qfq"], provider="tushare", adjust="native"
)
```

`native` 表示沿用所选字段定义：Tushare 的 `qfq` 字段明确是前复权，通达信的复权方式则未披露。Tushare 也接受与全部所选字段一致的 `none` / `qfq` / `hfq`；不一致会拒绝。跨复权字段只能用 `native` 分别保留既定定义。仅支持日频。

通达信的整数参数范围以 profile 为准，MA 的后四条均线默认参数为 0，表示关闭，对应值保留空；其他有效参数范围不是统一从 1 开始。厂商 `params_used` 必须与请求一致才接受；BOLL 仅暴露 `m`，不提供自定倍数。Tushare 参数固定，拒绝覆盖；其 BBI 文档不同复权字段的周期说明存在冲突，保留各字段原说明，不宣称参数一致。

单位按来源保留：通达信 VOL 与 OBV 为手，日线量价 `volume` 为股，1 手为 100 股；OBV 是有符号累计量，起点由厂商决定。ATR 是价格幅度（元），不是百分比。通达信 WR 使用 0–100 标度，越小越接近区间高点，不改成负值 Williams %R。VR 保留厂商原生标度，不能当作收益率；通达信以 % 标记其比率乘 100 的值，Tushare 标记无量纲，不据此强行互换。ASI、EMV 等未确认缩放的字段保留来源原值和 `unit_status` / `unit_note`，不伪造标准单位；其中通达信单位未披露分量可查询、不能保存为已确认个人定义。

`explore_metric` 可探索明确分量名称（如 `MACD DIF`），成功后保存一个分量及其默认参数。后续用保存字段 ID 查询，通达信省略 `parameters` 复用保存参数，显式参数局部覆盖默认值；Tushare 固定字段沿用其预计算定义。跨 profile 查询可省略参数；显式参数要求属于同一可调 profile。网页的个人字典“选到试查”会带入保存参数，字段列表可搜索 MA20 等名称。

Wind、iFinD、AKShare 的本轮接入与核实边界见[其他来源技术指标](docs/technical-other-sources.md)。Wind 历史与最新快照使用独立字段，快照不补齐历史日期；AKShare 当前已核实官方目录与安装版本没有可接入这套目录的原生历史数值接口，保留缺项，未启用本地计算。

当前不支持任意本地公式、任意 Tushare 参数、周线／分钟线，以及上述目录外未映射指标。通达信 EXPMA 的 EMA 别名不等于另一个独立 EMA 接口；通达信唐奇安接口只接上下轨，不冒称支持 Tushare TAQ 三轨；KTN、TAQ 三轨与四类天数项通过 Tushare 固定字段提供。缺项明确报告，不猜接口或补算。通达信日期受最近日线覆盖限制，声明上限 5000 根不代表已验证取满；Tushare 单次上限 10000 行，达到上限时按日期向前分页，最多 20 页，超限提示缩短区间。

本轮真实查询已验证通达信新增 25 族的返回结构，以及 Tushare 全 226 个源字段的一日非空返回；其余离线测试覆盖参数传递、分量与日期校验、保存复用和字典扩展。真实样本通过不代表所有股票、历史区间或跨厂商算法一致。
