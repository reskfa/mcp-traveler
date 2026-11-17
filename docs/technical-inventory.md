# 技术指标目录边界与 Tushare 来源映射

核实日期：2026-11-16。目录参照 Tushare 股票技术面因子专业版；本轮完整边界为 **78 个输出分量/固定周期项，226 个复权字段**，不是 78 种互相独立的算法。价格、估值和每日基本指标不计入本技术目录。

来源：[官方专业版](https://tushare.pro/document/2?doc_id=328)、[官方机器可读文档](https://tushare.pro/wctapi/documents/328.md)、[官方旧版](https://tushare.pro/document/2?doc_id=296)。目录只约束覆盖面；通达信与 Tushare 保留各自算法，不以数值相等作为正确性条件。

## 权限与真实小查询

专业版单次上限 10000 行；5000 积分 30 次/分钟，8000 积分以上 500 次/分钟。2026-11-16 使用本地已配置凭证，查询浦发银行 2026-11-12 至 13 的 8 个字段，成功返回 2 行；这确认当前账号可调用该接口，不代表实测了频率上限。未输出凭证。

输入支持 ts_code/trade_date/start_date/end_date；ts_code 与 trade_date 至少提供其一。周期为日线，预计算参数固定；文档未提供任意参数覆盖入口。

## 口径和容易误接的字段

- 专业版有不复权 bfq、前复权 qfq、后复权 hfq 三套值。MA/EMA/RSI 的后缀在周期之前，例如 ma_qfq_20；其余主要为 atr_qfq。
- kdj_qfq 为 J 分量，而非 kdj_j_qfq。小查询两日均满足 J=3K-2D；这是实测确认。旧版则明确叫 kdj_j。
- 旧版技术值只基于前复权，且说明其历史快照不更新，与 pro_bar 按 end_date 动态复权可能不一致。不能把旧版快照说明自动套到专业版；专业版文档未给出同等清晰说明。
- 专业版 BBI 三个复权字段的文档分别写 M4=20/21/22，参数说明冲突；不能笼统宣称它们都等于某一套统一参数。比较算法时须单独核实。
- MACD 柱是否乘 2、RSI/DMI/ATR 平滑初始化、OBV 起点、滚动标准差自由度、通道是否包含当日均不可仅从字段名确定。
- 专业版明确 vol 为手、amount 为千元，不能因此推定 OBV/EMV 等派生字段的缩放。下表单位列空白表示官方未明确承诺，不猜单位。

## 全量来源字段映射

本表逐项列出全部技术输出；字段名可作为后续覆盖清单的来源键。尚未建立或验证某家来源映射时，明确保留缺项，不以中文名称相似替代来源证据。

|目录项|Tushare 专业版 source fields|单位（文档明确）|
|---|---|---|
|asi|`asi_bfq`, `asi_hfq`, `asi_qfq`||
|asit|`asit_bfq`, `asit_hfq`, `asit_qfq`||
|atr|`atr_bfq`, `atr_hfq`, `atr_qfq`||
|bbi|`bbi_bfq`, `bbi_hfq`, `bbi_qfq`||
|bias1|`bias1_bfq`, `bias1_hfq`, `bias1_qfq`||
|bias2|`bias2_bfq`, `bias2_hfq`, `bias2_qfq`||
|bias3|`bias3_bfq`, `bias3_hfq`, `bias3_qfq`||
|boll_lower|`boll_lower_bfq`, `boll_lower_hfq`, `boll_lower_qfq`||
|boll_mid|`boll_mid_bfq`, `boll_mid_hfq`, `boll_mid_qfq`||
|boll_upper|`boll_upper_bfq`, `boll_upper_hfq`, `boll_upper_qfq`||
|brar_ar|`brar_ar_bfq`, `brar_ar_hfq`, `brar_ar_qfq`||
|brar_br|`brar_br_bfq`, `brar_br_hfq`, `brar_br_qfq`||
|cci|`cci_bfq`, `cci_hfq`, `cci_qfq`||
|cr|`cr_bfq`, `cr_hfq`, `cr_qfq`||
|dfma_dif|`dfma_dif_bfq`, `dfma_dif_hfq`, `dfma_dif_qfq`||
|dfma_difma|`dfma_difma_bfq`, `dfma_difma_hfq`, `dfma_difma_qfq`||
|dmi_adx|`dmi_adx_bfq`, `dmi_adx_hfq`, `dmi_adx_qfq`||
|dmi_adxr|`dmi_adxr_bfq`, `dmi_adxr_hfq`, `dmi_adxr_qfq`||
|dmi_mdi|`dmi_mdi_bfq`, `dmi_mdi_hfq`, `dmi_mdi_qfq`||
|dmi_pdi|`dmi_pdi_bfq`, `dmi_pdi_hfq`, `dmi_pdi_qfq`||
|downdays|`downdays`|交易日数|
|updays|`updays`|交易日数|
|dpo|`dpo_bfq`, `dpo_hfq`, `dpo_qfq`||
|madpo|`madpo_bfq`, `madpo_hfq`, `madpo_qfq`||
|ema_10|`ema_bfq_10`, `ema_hfq_10`, `ema_qfq_10`||
|ema_20|`ema_bfq_20`, `ema_hfq_20`, `ema_qfq_20`||
|ema_250|`ema_bfq_250`, `ema_hfq_250`, `ema_qfq_250`||
|ema_30|`ema_bfq_30`, `ema_hfq_30`, `ema_qfq_30`||
|ema_5|`ema_bfq_5`, `ema_hfq_5`, `ema_qfq_5`||
|ema_60|`ema_bfq_60`, `ema_hfq_60`, `ema_qfq_60`||
|ema_90|`ema_bfq_90`, `ema_hfq_90`, `ema_qfq_90`||
|emv|`emv_bfq`, `emv_hfq`, `emv_qfq`||
|maemv|`maemv_bfq`, `maemv_hfq`, `maemv_qfq`||
|expma_12|`expma_12_bfq`, `expma_12_hfq`, `expma_12_qfq`||
|expma_50|`expma_50_bfq`, `expma_50_hfq`, `expma_50_qfq`||
|kdj|`kdj_bfq`, `kdj_hfq`, `kdj_qfq`||
|kdj_d|`kdj_d_bfq`, `kdj_d_hfq`, `kdj_d_qfq`||
|kdj_k|`kdj_k_bfq`, `kdj_k_hfq`, `kdj_k_qfq`||
|ktn_down|`ktn_down_bfq`, `ktn_down_hfq`, `ktn_down_qfq`||
|ktn_mid|`ktn_mid_bfq`, `ktn_mid_hfq`, `ktn_mid_qfq`||
|ktn_upper|`ktn_upper_bfq`, `ktn_upper_hfq`, `ktn_upper_qfq`||
|lowdays|`lowdays`|交易日数|
|topdays|`topdays`|交易日数|
|ma_10|`ma_bfq_10`, `ma_hfq_10`, `ma_qfq_10`||
|ma_20|`ma_bfq_20`, `ma_hfq_20`, `ma_qfq_20`||
|ma_250|`ma_bfq_250`, `ma_hfq_250`, `ma_qfq_250`||
|ma_30|`ma_bfq_30`, `ma_hfq_30`, `ma_qfq_30`||
|ma_5|`ma_bfq_5`, `ma_hfq_5`, `ma_qfq_5`||
|ma_60|`ma_bfq_60`, `ma_hfq_60`, `ma_qfq_60`||
|ma_90|`ma_bfq_90`, `ma_hfq_90`, `ma_qfq_90`||
|macd|`macd_bfq`, `macd_hfq`, `macd_qfq`||
|macd_dea|`macd_dea_bfq`, `macd_dea_hfq`, `macd_dea_qfq`||
|macd_dif|`macd_dif_bfq`, `macd_dif_hfq`, `macd_dif_qfq`||
|mass|`mass_bfq`, `mass_hfq`, `mass_qfq`||
|ma_mass|`ma_mass_bfq`, `ma_mass_hfq`, `ma_mass_qfq`||
|mfi|`mfi_bfq`, `mfi_hfq`, `mfi_qfq`||
|mtm|`mtm_bfq`, `mtm_hfq`, `mtm_qfq`||
|mtmma|`mtmma_bfq`, `mtmma_hfq`, `mtmma_qfq`||
|obv|`obv_bfq`, `obv_hfq`, `obv_qfq`||
|psy|`psy_bfq`, `psy_hfq`, `psy_qfq`||
|psyma|`psyma_bfq`, `psyma_hfq`, `psyma_qfq`||
|roc|`roc_bfq`, `roc_hfq`, `roc_qfq`||
|maroc|`maroc_bfq`, `maroc_hfq`, `maroc_qfq`||
|rsi_12|`rsi_bfq_12`, `rsi_hfq_12`, `rsi_qfq_12`||
|rsi_24|`rsi_bfq_24`, `rsi_hfq_24`, `rsi_qfq_24`||
|rsi_6|`rsi_bfq_6`, `rsi_hfq_6`, `rsi_qfq_6`||
|taq_down|`taq_down_bfq`, `taq_down_hfq`, `taq_down_qfq`||
|taq_mid|`taq_mid_bfq`, `taq_mid_hfq`, `taq_mid_qfq`||
|taq_up|`taq_up_bfq`, `taq_up_hfq`, `taq_up_qfq`||
|trix|`trix_bfq`, `trix_hfq`, `trix_qfq`||
|trma|`trma_bfq`, `trma_hfq`, `trma_qfq`||
|vr|`vr_bfq`, `vr_hfq`, `vr_qfq`||
|wr|`wr_bfq`, `wr_hfq`, `wr_qfq`||
|wr1|`wr1_bfq`, `wr1_hfq`, `wr1_qfq`||
|xsii_td1|`xsii_td1_bfq`, `xsii_td1_hfq`, `xsii_td1_qfq`||
|xsii_td2|`xsii_td2_bfq`, `xsii_td2_hfq`, `xsii_td2_qfq`||
|xsii_td3|`xsii_td3_bfq`, `xsii_td3_hfq`, `xsii_td3_qfq`||
|xsii_td4|`xsii_td4_bfq`, `xsii_td4_hfq`, `xsii_td4_qfq`||
