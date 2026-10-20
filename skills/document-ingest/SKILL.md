---
name: document-ingest
description: Convert source documents into readable Markdown with visible original coordinates and fidelity artifacts.
version: 1.1.0
---

# 文档处理员：格式转换

本技能只负责原件格式转换，不抽业务事实、不判断公司归属、不修改原件。
使用注册工具 `sam_document_ingest`，参数严格为 `{file_hash, format}`；原件和保存位置由宿主注入。
不得提供路径、命令、公司参数；不得自行执行 shell 或写正式记忆库。

一份 Word 原件对应一份 Markdown，正文与原件顺序一致，标题层级对应原件，表格保留独立列及合并关系，坐标可见，图与嵌入对象挂原位链接。
转换交付包含 Markdown、结构地图、资产/超链接清单、保真自检及独立引擎结构结果。
未支持的图元/对象保留原对象并显式提示回看原件，不得假装已生成快照。
机械自检给出正文覆盖口径、表格行列、媒体对象清单及告警。告警不等于员工质检通过。

宿主随后为文档处理员派独立的保真质检任务，规则见 QUALITY.md；只有该任务提供受控的离线只读核验通道，当前转换任务不能自行执行代码。
质检不通过，不进入内容分析；带坐标问题单重转到限额为止，仍无法解决则交人工处理。

## 本地引擎

工具在本地选择 Docling/MinerU，不把原件字节发送给转换引擎的远端服务。
Office 优先 Docling；PDF/图片优先本地 MinerU，失败后可尝试另一可用本地引擎。
本地依赖未就绪、服务不可用或转换失败必须明确报失败，不能假装有正文。
Word 页码若无法确定，只保留真实结构坐标，不编页码；高清原件页面快照由宿主受控离线生成。

## 工具发现

1. `tool_search({"query":"parse an uploaded document"})`。
2. 使用返回的精确工具名 `sam_document_ingest`，不自造前缀。
3. `tool_describe({"id":"sam_document_ingest"})`。
4. `tool_call({"id":"sam_document_ingest","args":{"file_hash":"<64 lowercase hex>","format":"docx"}})`。

禁止空参数、路径、shell 文本或宿主目录；错误时重新发现/描述工具，不猜参数。
MinerU 若部署在线服务，应保留其许可证和要求的归属说明；本地工具不是在线服务。
