下面是已经计算好的指标表（同比、环比、vs 基准），数字以此为准，不得重新计算或改写。
针对表中每个指标，解释变动原因：
1. 把原因拆到以下类型：量、价、产品或客户结构、单位成本、费用投入、一次性项目、汇率、其他。
   标出哪一个是主因。
2. 每条原因必须附 evidence_quote（原文原句，不超过 200 字符）和 source（press_release / prepared_remarks / qa）。
   找不到原文依据的推测，is_inference 填 true，并在 reasoning 里写明依据的是哪些数字。
3. 如果管理层的解释与指标表的数据不一致（例如称需求强劲，但库存或应收快速上升），写入 conflicts。
4. 每个指标的 summary 不超过 120 个汉字，用中文。
只输出 JSON：
{
  "metrics": [
    {
      "metric": "gross_margin",
      "summary": "",
      "drivers": [
        {"type": "价", "direction": "+", "is_primary": true, "description": "",
         "evidence_quote": "", "source": "", "is_inference": false, "reasoning": ""}
      ],
      "conflicts": []
    }
  ]
}
