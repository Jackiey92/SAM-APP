# SAM-APP · Startup AI Manager

> 目录导航修订：2026-10-22T22:38+08:00（Asia/Shanghai）。

> 代码仓库标识：SAM-APP；产品文档权威仓库：SAM-DOCS。

> 面向初创公司的 **Agent-Native 经营 Harness**：一个由 AI Agent 主动维护的「活的公司状态面」，把 **外部信号 × 公司上下文 × 建议行动** 三者耦合，完成过去需要公司几个部门联合才能做的判断。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

🔗 **在线体验（无需下载）**：https://jackiey92.github.io/startup-ai-manager/

## 这是什么

第一次创业的创始人，往往「不知道自己不知道」：兄弟合伙口头分股、注册资本随意填写、公私账不分、技术未做权属约定、第一轮融资时资料混乱。这些风险大多死在 **板块之间的缝隙**，而单点工具（财务软件、股权工具、传统 ERP）各自为政，没有任何一方能跨域预警。

Startup AI Manager 不是在传统 ERP 上加一个聊天框，也不是又一个通用问答助手。它的核心是：

- **Agent 是当前原型的主要执行者**：通过对话与文件驱动，创始人只在关键节点「拍板」；邮件等外部连接器仍属后续范围；
- **围绕一个持久、可查询、跨域的「公司模型」运转**，而不是一问一答、聊完即走；
- **每一个出现在界面上的数字都可溯源到源文件**，并区分「事实 / 口述 / 推断」；
- **外部数据与内部数据分区隔离、单向清关**，防止外部内容污染公司账本。

## 核心主张

> **Harness 时代，模型是引擎，记忆才是车。** 模型可随时替换，真正的壁垒是「敢不敢信它记住的东西」——可信的写、可溯的读、随时间复利的公司记忆。

## 形态观

对话窗口不是 Harness 的最终形态：它强大、通用，却是 Agent 时代的 **CLI**；本项目默认提供一张主动推送、空间化的 **「公司状态 GUI」**，对话作为开放式追问的兜底入口。

## 架构总览（溯源优先 + 信任边界）

```
┌─────────────────────────────────────────────────────────┐
│  前端可视化 UI（活状态面）   每个元素 = 一个可点的溯源句柄      │
└──────────────▲──────────────────────────────────────────┘
               │ 读（只投影，带滤镜）
┌──────────────┴──────────────────────────────────────────┐
│  事实层 / 结构化记忆系统（公司模型）                         │
│  只追加 · 带出处 · 带版本 · 区分 事实/口述/推断              │
└──────────────▲──────────────────────────────────────────┘
               │ 写（只能走「事务网关 + 校验闸门」）
┌──────────────┴──────────────────────────────────────────┐
│  AI Harness 运行空间（隔离沙箱 · 内置企业基线 · 可接工具）    │
└──────────────▲──────────────────────────────────────────┘
               │ 只读引用 / 受控
        ┌──────┴───────┐
     原文层(不可变)   外部数据区(不可信，需清关)
```

八条工程纪律：**原文不可变 · 事实只追加 · 写入走闸门 · 读取带滤镜 · 外部先检疫 · 引用必回源 · 动作人签字 · 正确性靠评测。**

## 当前阶段

这是一个**开发期的单主 Agent 原型**，不是只有架构图的空仓库：上传、确定性解析、证据暂存/2a 落库、OpenClaw 主 Agent 引导和受限记忆工具已接通，并已在本地真实 OpenClaw + Token Plan 环境完成端到端验收。

仍在规划或尚未向产品流开放的内容包括：更完整的第三层认知编排、长期生产部署/多用户治理、以及把候选事实主动晋升为 verified 事实的正向写入流程（`promote` 不作为当前已交付能力宣传）。

## 技术底座

- **Agent 运行时**：[OpenClaw](https://github.com/openclaw/openclaw) —— 已实际接入，`sam-leader`（显示名 SAM Leader）是当前唯一的主 Agent；其插件工具提供受公司/线程范围约束的记忆与对话查询。
- **模型路由**：开发期默认经 Token Plan 的 OpenAI-compatible 路由使用 `qwen3.8-max`；模型名、端点与密钥均由环境变量/manifest 注入，可替换，仓库不保存密钥。
- **记忆底座**：本地 SQLite 事实账本 + sqlite-vec 向量投影 + 离线 BGE；业务层依赖 `MemoryProvider`。独立 `sam://kv/co/...` 公司 JSON KV 与 `sam://kv/rt/...` 会话运行 KV 均不 embedding，不进入事实检索。旧底座已摘除，无后台记忆服务依赖。
- **公司事实层**：自研的事实/来源约束与派生结构，保持与记忆实现解耦。

> 选型理由与许可证边界见 [技术选型文档](./docs/architecture/08-memory-store-selfhosted-design.md)。

## 本地记忆运行（M4）

无需 `SAM_MEMORY_ROOT_URI`，默认 `SAM_MEMORY_BACKEND=localvector`。遗留 `ov` 配置显式报错；云只读优先空读/写拒绝。离线模型缓存需预装，不能静默下载或回退外部服务。
详见 [本地记忆指南](./docs/local-memory.md)。旧数据不迁移，新地址缺失按业务空态处理；物理历史数据/工具/进程未在 M4 清理。

## 目录结构

```
.
├── README.md、LICENSE          # 项目入口与许可证
├── app/                        # 后端主包
│   ├── facts_tree/             # 公司事实树、版本与发布
│   ├── document_read/          # 文件阅读与笔记处理
│   ├── document_quality/       # 文档质量检查
│   ├── employees/、harness/    # 员工执行与运行时适配
│   ├── memory/、storage/      # 本地记忆与原件存储
│   └── dashboard/、entities/、db/ 等基础模块
├── webapp/                     # Flask 服务、页面与静态资源
├── skills/                     # 员工技能与文档处理约定
├── harness-openclaw/           # Agent workspace、插件与配置模板
├── scripts/                    # 启动、维护与开发验证脚本
├── tools/                      # 隔离环境构建和启动工具
├── profiles/、schemas/        # 环境配置与数据 Schema
├── sam、sam-manifest.yaml      # 命令入口与运行声明
├── requirements*.txt           # 应用、云端及质检依赖
├── tests/                      # 回归测试与夹具
├── data/                       # 运行数据与受版本管理的样例
├── docs/                       # 代码运行说明与历史资料，见 docs/README.md
├── poc/                        # 历史验证原型
├── prototype/                  # startup-ai-manager.html 前端原型
└── index.html                  # 在线原型跳转入口
```

## 已验收的原型能力

- 上传资料后走**确定性本地解析**：Office 优先 Docling，PDF/图像优先 MinerU；首选引擎不可用时自动回退，并记录 warning。
- 解析状态如实返回：成功解析并写入原始记忆层为 `parsed` / `stored_2a`；引擎不可用或损坏文件为 `parse_failed`，不会把空 manifest 伪装成成功。
- `sam-leader` 主 Agent 异步基于已解析的正文片段和 L2 URI 生成导入建议；同一公司的并发请求单飞合并，未完成时返回可轮询的 `202 generating`。
- 引导保留证据中的原始数值字面量与出处，不由模型自行换算单位；对同名、数值冲突的资料会提示口径待核。
- 文档解析只产出带 locator 的 L2 证据。候选事实自动晋升/正向写入 verified 2b 仍是待完成事项。

## 本地运行（开发原型）

依赖 Python 3.14+、本地 OpenClaw 运行时，以及你自己的 Token Plan/OpenAI-compatible 模型 API key。不要把密钥写入仓库或日志。

```bash
git clone https://github.com/Jackiey92/startup-ai-manager.git SAM-APP
cd SAM-APP
python3 -m venv .sam-isolated/venv
.sam-isolated/venv/bin/pip install -r requirements.txt

export SAM_LEADER_MODEL_API_KEY='从你的安全环境注入'
export SAM_LEADER_MODEL=qwen3.8-max
export SAM_VENV_BIN="$PWD/.sam-isolated/venv/bin"
export SAM_TOOL_BRIDGE_PYTHON="$PWD/.sam-isolated/venv/bin/python"
# 按本机 OpenClaw 安装位置设置 SAM_NODE_BIN 与 SAM_OPENCLAW_ENTRY。

"$SAM_TOOL_BRIDGE_PYTHON" scripts/prepare-openclaw-runtime.py
(cd webapp && "$SAM_TOOL_BRIDGE_PYTHON" app.py)
```

`prepare-openclaw-runtime.py` 会准备 state、技能与插件发现目录；随后由 `webapp/app.py` 启动 Flask。仅查看静态界面可打开 [`prototype/startup-ai-manager.html`](./prototype/startup-ai-manager.html) 或在线体验链接；上传/Agent 功能需要本地运行时和模型密钥。

## 唯一前端原型模板（设计基准）

`prototype/startup-ai-manager.html` 是项目**唯一权威的前端原型模板**，由范范于 2026-09-29 拍板确认（对应云盘 `startup-ai-manager_最新前端.html`，二者内容哈希一致）。后续所有前端打磨、模块补齐、视觉与交互对齐都以该文件为基准；其它历史草图或临时页面不作为依据，真机功能仍只渲染后端真实数据。

旧版 `.doc` 与 `.ppt` 需要本机 LibreOffice 进行**本地**无头转换（不会上传原件）：Ubuntu/WSL 安装 `libreoffice-core libreoffice-writer libreoffice-impress`，可选用 `SAM_SOFFICE_BIN` 指向 `soffice` 可执行文件。缺少该依赖时，旧格式上传会返回部署依赖错误，不会伪装为解析成功。

## 文档

现行产品设计以独立的 **SAM-DOCS** 仓库为准。这里保留代码运行说明与历史设计、研究和施工证据。

- [文档导航与归档说明](./docs/README.md)
- [M5历史施工汇报（原根目录report.md）](./docs/handoffs/2026-10-13-m5-delivery-history.md)

以下为仓库内历史设计与研究入口：

- [产品整体思路](./docs/product/01-overall-thinking.md)
- [产品形态论：对话之后](./docs/product/02-beyond-chat.md)
- [架构总览](./docs/architecture/01-system-architecture.md)
- [可信记忆系统](./docs/architecture/02-memory-system.md)
- [信任边界与外部数据清关](./docs/architecture/03-trust-boundary.md)
- [技术选型：运行时与记忆底座](./docs/architecture/08-memory-store-selfhosted-design.md)
- [竞品与市场调研](./docs/research/01-competitive-landscape.md)

## 开源与边界

- 采用 [MIT License](./LICENSE)。
- 总账 / 税务 / 成熟 ERP 模块倾向 **外挂**（记账与执行器官），本项目聚焦自研「公司模型」与跨域编排（大脑）。
- 当前自托管记忆实现位于本仓；历史底座选型仅作为决策回溯材料，不是运行依赖。
- AI 定位为辅助分析、风险提示与资料齐备度检查，不替代律师 / 会计师 / 税务师的专业意见；高风险动作默认「Agent 起草 + 人来拍板」。
