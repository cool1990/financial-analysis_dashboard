下面是业绩电话会 Q&A 的若干轮问答。将每个问题拆解为一条记录，只输出 JSON 数组。每条包含：
- exchange_id：输入中给出的编号
- analyst、firm
- topic：从话题清单中选；都不匹配时新建一个简短中文话题名，并把 is_new_topic 设为 true
- question_summary：一句话中文，写清分析师真正想知道什么
- answer_summary：不超过两句中文
- new_numbers：回答中给出、但新闻稿里没有的数字，每项含数字原文和含义；没有则为空数组
- directness：direct（直接回答）/ partial（部分回答）/ evasive（回避）
- evasion_note：directness 不是 direct 时，写明没回答的是什么
- tone：positive / neutral / cautious（管理层回答的语气）
- answer_quote：最能代表管理层态度的一句原话，不超过 30 个英文单词
规则：只根据输入文本，不要补充外部知识。必须输出非空 JSON 数组；若某轮信息不足，仍返回该 exchange_id 的对象，question_summary/answer_summary 用中文说明「文本不足」。
话题清单：{topics}
新闻稿已披露的数字：{press_release_numbers}
