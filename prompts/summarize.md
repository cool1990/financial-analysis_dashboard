基于以下结构化结果（数字以此为准，不得改写），只输出 JSON：
1. headline：一句话概括本季财报的核心变化或核心矛盾，不超过 40 个汉字。
2. key_findings：3 条最重要的发现，按对股价的影响程度排序，每条引用支撑它的指标名或 Q&A 编号。
3. prior_watchlist_review：逐条评估上季跟踪清单，status 为 confirmed / refuted / pending，各附一句证据。
4. next_watchlist：3–5 条下季跟踪问题。每条必须可以用下季数据验证，
   写明 metric_key（若有）、验证条件和阈值，例如“FQ1 Non-GAAP 毛利率是否达到指引中值 42.5%”，不要写“关注毛利率”。
