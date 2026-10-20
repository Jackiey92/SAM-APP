---
name: finance-fact-extraction
description: Extract finance fact candidates without changing digits or units.
version: 1.1.0
metadata:
  sam:
    roles: [finance_analyst]
    visibility: workspace
---

# 文档管理员工：完整公司事实初筛技能

从给定的单个 self 或员工明确并表的 related 证据块提出候选，不做事实确认。只逐字复制原文中的数字和
单位（元、万元、亿元、%），禁止换算；期间没有明确年份时返回
`unspecified`，不要从上下文臆造。计划、预计、目标等内容应保留为候选，交由
后端待办策略处理。返回严格 JSON：`{"candidates": [...]}`，每项包含
`metric/value/unit/entity/period/quote`。`quote` 使用人话原文片段；不要猜测
或构造机器页码、定位符、文件哈希，后端会从匹配的 self 块补齐坐标。
必须返回裸 JSON 对象或数组，禁止使用 Markdown 代码围栏（例如 ```json ... ```）。
如果传输信封或 JSON 无法解析，宿主会在不改变原文与请求的前提下有限重试；不要
借重试补数字、改引用或改变候选语义。

如果原文不足以形成可逐字核验的候选，必须返回空 `candidates`，并附
`unresolved` 说明卡点；不要用常识补数字、实体、期间或单位。示例：
`{"candidates": [], "unresolved": {"reason": "missing_source_grounding", "blocking": "..."}}`。

输入可能是宿主传入的 JSON 上下文，形如
`{"source_block":"...", "attribution":{"classification":"self|related", "subject":"...", "relation":"..."}}`。
只从 `source_block` 逐字引用和提取；`attribution` 是文件处理员工已经
作出的主体归属交接，不是让财务员工重判。若其中有完整 `subject`，候选的
`entity` 必须沿用该主体（不得输出字面量 `unspecified`）；若主体确实为空，
才可如实返回 `unspecified` 并交经理处理。关系信息只用于保留主体边界，不能
把子公司数字并入母公司。不要把宿主上下文包装字段写进 `quote`。

## Semantic finance knowledge

Recognize equivalent wording by meaning rather than requiring a fixed alias:
revenue/sales income/operating income, net profit/profit attributable to
owners, gross margin, cash and equivalents, accounts receivable, total assets,
operating cash flow, R&D spend, registered capital, financing, customers,
suppliers, orders, capacity and litigation. Keep the employee's chosen metric
label and quote; the application must not rebuild an alias table.

For consolidated reporting, separately identify the host's consolidated scope,
a subsidiary's standalone scope, and related/foreign entities. The employee
decides consolidation from report language and evidence; a backend relationship
word list is not authoritative. Keep parent and subsidiary facts as separate
entity records and preserve the relationship in the candidate.

## 完整性

此工序是 2B L2 初筛，不只取财务数字。保留归属于标的公司的全部有据公司信息：
主体基本信息、注册地、主营业务、跨期财务、各客户与对应订单、核心员工及职务、
股东与对应比例等都可用文本或数值候选表达。每个独立字段用员工确定的明确标签，
不要把不同客户订单或不同员工、股东合并为同一个 metric/entity/period 坐标，
避免信息冲突或覆盖。非数字 value 设置 value_type 为 string。
公司资料中客户/员工/股东是事实的对象，不要把主体 entity 从已归属的标的公司
偷换成客户公司或个人。critical/review_reason 由你按不确定性给出，后端不判指标类别。
不得因非财务而静默丢信息；无法判定的条目明确给出 needs_review/review_reason。

## 可选事实模块

候选可附 `module_key`，由员工自行在 `info/bizov/hr/prod/purch/sales/mkt/fin/fund/legal`
九个事实模块中判断（财务事实通常为 fin，仍由员工判断）。拿不准时省略或留空，
进入待归类；不改变既有事实判定、引用、数字与单位契约。这不是文件归档分类，
不得根据文件层 module 自动换算。
