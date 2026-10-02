抽取管理层给出的所有前瞻性指引，只输出 JSON 数组。每条包含：
- metric_key：从给定标准键清单中选择，不匹配则填 "other"
- metric_label：原文中的指标名称
- period：适用期间，写成原文表述（如 "fiscal Q1 2027"、"fiscal 2027"、"calendar 2026"）
- type：numeric（有具体数字或区间）/ directional（只有方向，如“环比增长”）/ qualitative（对市场、供需、定价、宏观的判断）
- low_raw / high_raw / point_raw：原文数字拷贝，如 "$12.2 billion"、"± $300 million"、"42.5%"；以“中值 ± 区间”形式给出的，填 point_raw 和 plus_minus_raw
- basis：GAAP / non-GAAP / unspecified
- direction：仅 directional 类型填写（up / down / flat）
- statement：一句话中文概括
- source：press_release / prepared_remarks / qa
- source_quote：原文原句，不超过 200 字符
规则：
1. 只抽取管理层的说法，不要把分析师提问中的数字或假设当作指引。
2. 覆盖范围：财务指标、经营数据、订单或 backlog、资本开支、对行业供需、定价、竞争和宏观的判断。
3. 定性判断也要收录，例如“预计 2027 年 HBM 供应持续紧张”。
4. 禁止计算中值、禁止换算单位。
5. 输出紧凑：每条 statement 一句话即可，不要复述大段原文；整段 JSON 尽量短。
标准键清单：{guidance_keys}
