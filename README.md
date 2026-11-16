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

具体客户端的配置位置可能不同；远端云端 Agent 无法直接访问你电脑的 localhost，本版面向本机 Agent。

提供五个工具：`data_catalog`、`query_daily_prices`、`query_valuations`、`query_financials`、`provider_status`。

可直接提问：

> 用 ashare-gateway 查贵州茅台 2025-11-03 到 2025-11-05 的不复权日线，再查 2024 年年报营业收入和归母净利润，注明来源。

查询只返回前 100 行预览及完整结果路径，避免把大量数据放入上下文。查看字段时调用 `data_catalog`，无需重复探索上游工具。

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

[总字典表](docs/data-dictionary.md) 列出全部 193 个分类字段（量价 7、估值与每日指标 17、财务 169）在五家来源的取法、返回列、单位转换及缺项。

唯一配置源是 `gateway/data/dictionary.json`，人读的 Markdown 由它生成；取数执行器、数据目录和字段解析也读取同一份 JSON。

- `datasets.*.fields`：统一字段含义、单位、别名及每家来源的绑定。
- `recipes`：SDK 方法或 MCP 工具、参数模板、分页、备用接口及问句模板。
- `gateway/dictionary.py`：校验字典并按请求字段生成调用计划。
- `gateway/acquisition.py`：执行计划、标准化数据、合并同一来源的多个接口返回。
- `gateway/providers.py`：凭证、MCP 传输与 SDK 初始化，不再维护业务字段列表。

字段绑定指定 `recipe`、`columns`、可选 `multiplier`、`prompt` 等。一个接口的多个字段合并调用；独立接口分别调用。缺少取法的字段不发出猜测请求。新增已有接口的字段通常只需编辑字典；接入新的返回结构或协议仍需扩展执行器。

修改字典后重启服务，并同步文档：

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
