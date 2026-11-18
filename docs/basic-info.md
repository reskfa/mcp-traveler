# 股票基本信息

本轮增加 `basic_info` 类别，共 56 个字段。与量价、财务等类别使用同一本字典、同一套路由、结果存储和 MCP 服务。

## 四组内容

| 分组 | 内容 |
| --- | --- |
| 公司与证券档案 | 简称、全称、英文名、上市状态与日期、成立日期、企业所有制、实控人与其性质、地址、主营、简介、官网、员工、注册资本等 |
| 证券名称历史 | 带起止和公告日期的简称变更记录；无日期的曾用名清单独立保存 |
| 行业归属 | 申万一级至三级名称和代码、归属记录；Wind一级至四级；同花顺一级；Tushare基础行业、巨潮行业、通达信研究行业与证监会行业分别保存 |
| 板块与标签 | 同花顺全类别板块、N类概念；Wind标签；通达信主题；巨潮/通达信指数名单 |

档案与分类表示来源**当前可用**的版本，不是任意历史时点的还原。`date` 固定为 null，取数时间用 `fetched_at`。历史列表仅在来源返回时填起止日期，不推断缺失日期。

## 如何使用

网页“试查数据” → “股票基本信息”，输入 `300308.SZ`，可选择四组之一或勾选单个字段。基本信息继承整体偏好，也可在“路由顺序”的细分类别中单独设置。

MCP 新增 `query_basic_info`：

```json
{
  "symbol": "300308.SZ",
  "fields": ["company_name", "ownership_type", "name_history", "sw_industry_l1", "ths_concepts"],
  "provider": "auto"
}
```

HTTP 使用同一个 `POST /api/query`，在上述参数中加 `"dataset": "basic_info"`。省略 fields 使用常用项；`["*"]` 查询全部。`data_catalog(dataset="basic_info", field="name_history")` 可查取法，`search_metrics` 可搜索字段。新增的是文本/列表查询；未知指标探索仍限数值指标。

字段类型为 `string`、`date`、`number`、`records`；`record_schema` 说明列表内各键类型。CSV 中列表使用 JSON 字符串，完整 JSON 下载保留结构。`field_sources` 按字段标明来源；原始返回保留到本机 `local/results/`。

## 已接入取法与实测

2026-11-17，用现有配置对 `300308.SZ` 完成五家真实取数：

| 来源 | 字典配置的取法 | 本轮确认的内容 |
| --- | --- | --- |
| Tushare | stock_basic、stock_company、namechange、index_member_all、ths_member + ths_index | 公司档案、2条证券名称记录、申万三级及代码、全类别板块与N类概念 |
| Wind | get_stock_basicinfo；分档案、地域、曾用名、行业/标签、经营信息的固定模板 | 公司档案、民营企业、曾用名、申万三级、Wind四级、板块标签 |
| AKShare | stock_profile_cninfo（巨潮资讯） | 公司档案、曾用简称、巨潮行业、入选指数 |
| iFinD | get_security_indicators；每问不超过5项 | 公司名称、实控人及性质、地区/地址、曾用名、申万/同花顺一级行业、同花顺概念 |
| 通达信 | tdx_api_data：tdxf10_gg_gsgk / fixedTag=0，tdxf10_gg_zxts / fixedTag=gsgy | 公司档案、曾用名、通达信/证监会行业、主题与指数名单 |

调查和真实结果在本机 `local/basic_info_research/`；此目录被 Git 忽略，不会随项目发布。通过真实 MCP 客户端发现10个工具，并用自动路由分别从 Wind、iFinD、Tushare 补齐公司名、企业性质、名称历史和两类板块，CSV/JSON/原始结果下载均已验证。另在网页实际查询并显示了证券名称历史。

## 刻意分开的口径

- `ownership_type` 是企业所有制；`controller_type` 是实控人性质。iFinD 的 `企业类型=大型企业` 单列为 `ifind_enterprise_type`，不填成所有制。
- `name_history` 是证券简称变更，不是公司工商名称变更；`former_names` 不承诺日期或历史完整性。
- `ths_boards` 是全类别板块；`ths_concepts` 仅N类。即便N类也可能包括指数样本、融资融券资格等标签，不能声称都是产业概念。
- 不跨分类体系补行业；省份原文“山东”和“山东省”保留来源原文。行业版本未返回时不推断。
- 列表作为整个字段保留优先来源，不拼接不同厂商的名单；缺失字段继续走路由。不能把某来源名单较短直接判定为错误或缺漏。
- Tushare注册资本由万元转为元。AKShare/通达信本次返回未明确金额数量级，暂不映射；不是用两家的数值碰巧相同来猜单位。
- 未配置或未取到的项为 null，既不填零，也不承诺该数据源永远没有这个字段。

## 扩展方式

`gateway/data/dictionary.json` 是唯一取法配置。每项包含 `data_type`、`group`、来源 recipe、列名、类型转换和可选列表规则；recipe 配置 SDK/MCP 方法、固定问句、证券身份列以及可选目录关联。

`gateway/basic_info.py` 只实现通用的标量、记录列表、分隔字符串列表、目录关联，不按字段 ID 分支。扩展现有返回表中的字段只需改字典；新返回结构才需要新增通用解析规则。`gateway/acquisition.py` 按字典分派，单个基本信息方案失败保留已取结果；`gateway/core.py` 在没有交易日期的情况下仍按证券逐字段补缺。

参考文档：[Tushare股票列表](https://tushare.pro/document/2?doc_id=25)、[上市公司基本信息](https://tushare.pro/document/2?doc_id=112)、[名称变更](https://tushare.pro/document/2?doc_id=100)、[申万行业成分](https://tushare.pro/document/2?doc_id=335)、[同花顺板块](https://tushare.pro/document/2?doc_id=259)、[板块成分](https://tushare.pro/document/2?doc_id=261)、[AKShare巨潮接口源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_profile_cninfo.py)。三个MCP以本次实际工具目录和返回表核实。
