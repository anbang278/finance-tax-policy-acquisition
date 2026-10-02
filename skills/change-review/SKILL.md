---
name: change-review
description: 将采集器修复候选、证据、修改文件与实际验证状态写成可供人工审查的变更说明。
---

# 修复候选审查

读取 `ftr candidate report --candidate ID`、候选 diff、失败证据和受影响资料。说明触发问题、修改范围、可能改变的资料行为、风险、尚需的测试和回退安排。测试数量和通过状态只能引用真实执行报告。

代码补丁仍仅做静态范围检查，明确“候选代码测试未运行，禁止发布”。受限规则另读 rules/candidates 下制品与实际报告，区分 RULE_OFFLINE_VALIDATED、RULE_VERIFIED、RULE_ACTIVE、RULE_ROLLED_BACK；真实来源验证缺失不能启用。不得伪造报告、审批收据或把本机规则启用称为正式治理发布。
