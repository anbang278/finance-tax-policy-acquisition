---
name: research
description: 基于已验证的财税原文、解读和发布消息撰写带定位引用的研究摘要，并标注证据缺口。
---

# 财税资料研究

如果研究请求需要新采集政策资料，先确认用户指定了可确定的时间范围；仅说“最近”或未说明期限时，反问起止日期或明确长度，收到答案后再交给 `controller` 发起采集。查询已有资料时说明资料库目前的覆盖范围，不将“库中最新”直接称为“官网最新”。

用 `ftr search --query 关键词` 检索已验证资料；需要看历史或隔离记录时明确使用 `--include-limited`，不得将其作为正式结论。研究草稿用 `ResearchDraft` 结构表达问题、截至日期、陈述、材料类型、引用和缺口；调用 `ftr research submit --file FILE` 做引用核验和渲染。

每项陈述引用具体资料版本、证据 ID 和实际原文摘录。区分政策原文、官方解读、发布消息与推断。同一政策被两站转载不算两个独立来源。未解析附件、未知效力、缺少企业事实都须直说；不把“未找到废止信息”写成“仍然有效”。

## 专题研究输出

先根据问题选择文号与主题检索词，记录每次实际查询和召回记录。通过research prepare --context传入所属期、申报日、地区、主体事实和指定法源。每条陈述保留条件、例外、时间依据和完整的支持片段，转引应包含被引文件及执行关系。

首次research submit得到定位结果与precheck_input_digest。使用独立AI核对同一背景和依据，对每条原样陈述给support、conditions、temporal、authority四维checks，明确supported、contradicted或insufficient，绑定该摘要后再次提交。输入发生变化则重新预审。缺来源与关键事实时填写missing_inputs和limitations，保留待核实状态。
