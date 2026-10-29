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
# 先按下节安装 manifest 固定的受管运行时；默认无需设置 Node/入口覆盖。

"$SAM_TOOL_BRIDGE_PYTHON" scripts/prepare-openclaw-runtime.py
(cd webapp && "$SAM_TOOL_BRIDGE_PYTHON" app.py)
```

`prepare-openclaw-runtime.py` 会准备 state、技能与插件发现目录；随后由 `webapp/app.py` 启动 Flask。仅查看静态界面可打开 [`prototype/startup-ai-manager.html`](./prototype/startup-ai-manager.html) 或在线体验链接；上传/Agent 功能需要本地运行时和模型密钥。

### CHG-070 V2：一次性安装受管运行时

> 修订：2026-10-29T18:36+08:00（Asia/Shanghai）。以下是操作说明；版本唯一来源为 `sam-manifest.yaml`。

在 Linux x64 上，先准备 GnuPG 和可信导出的 Node 发布公钥（签名指纹必须为 `5BE8A3F6C8A5C01D106C0AD820B1A390B168D356`）。在线模式还需 `curl`，沿用 `HTTPS_PROXY`/`https_proxy`/`ALL_PROXY`；只接受精确官方 HTTPS 地址，拒绝重定向。安装器不联网取公钥；不传 `--node-release-key` 时，从当前 GPG keyring 只读导出该指纹，缺失即失败。

```bash
# 在线：下载固定 OpenClaw/Node tarball、SHASUMS256.txt 和 .asc。
python3 scripts/install-managed-runtime.py --node-release-key /trusted/node-release-key.asc

# 离线：目录内必须放齐下面列出的四个官方文件；完全不调用下载器。
python3 scripts/install-managed-runtime.py --staging-dir /trusted/runtime-artifacts --node-release-key /trusted/node-release-key.asc

# 安装成功后，只读验收（只执行受管 Node 的版本/平台探测）。
python3 scripts/prepare-openclaw-runtime.py --check
```

当前契约所需离线文件：`openclaw-2026.10.7.tgz`、`node-v24.22.1-linux-x64.tar.gz`、`SHASUMS256.txt`、`SHASUMS256.txt.asc`。公钥可放在同一目录，须通过参数指定。在线与离线均重新计算 OpenClaw SHA-512，与 manifest SRI 比较；Node 校验 GPG clearsign 的固定签名指纹、签名正文与独立 SHASUMS 文本一致、文件名对应的 SHA-256 与审阅过的官方摘要一致，以及 tarball 实际摘要一致。缺 GPG、公钥、签名或任何证据不符均失败关闭，没有跳过验证参数。

安装在默认 `.oc-runtime`，锁放在同级 `.oc-runtime.installer/install.lock`（`flock`，进程退出自动释放，默认等待 60 秒）。私有 staging 为 `0700`，账本为当前用户拥有的单硬链接普通文件 `0600`。拒绝越界路径、特殊文件和不安全链接；保留经验证的根内文件软链以维持 npm/npx 的相对导入语义，硬链接复制为普通文件。schema 1 账本覆盖全部部署文件（账本自身除外），包括 OpenClaw 入口、包声明、依赖文件、Node 及软链别名和目标。原有 preflight 完整通过后才发布：首次 `rename`，重装用 Linux `renameat2(RENAME_EXCHANGE)` 原子交换非空目录；不支持交换就保留旧目录并失败。重复安装安全重建，不在 live root 内覆盖文件。

`bash scripts/setup.sh` 保留原来的 Python 依赖安装行为，默认不安装运行时；显式使用 `bash scripts/setup.sh --install-managed-runtime --node-release-key /trusted/node-release-key.asc` 才追加在线安装，也可同时传 `--staging-dir`。已备好 Python 环境的隔离机直接调用上面的安装脚本，避免 setup 的 pip 步骤。`run.sh` 保持先 prepare/preflight 后启动的流程；安装器只做 Node 元数据探测，不启动 OpenClaw/Gateway、不执行 npm 生命周期或自动更新。

审阅者提供的官方证据（核验日期 2026-10-29，本实现未重新联网核验）：OpenClaw 官方 tarball 为 103,494,479 字节，SHA-512 SRI 与 manifest 一致，npm SHA-1 为 `7aaaa1b0a91f79ce2f17236b09869c25f579e209`；包内无 `gitHead`。上游注解 tag `v2026.10.7`（tag object `5c101ce5a0637df375c5f6ae49d44d0d27cc26b1`）解引用至批准的完整 commit；npm SLSA v1 attestation 记录 GitHub Actions github-hosted builder、`.github/workflows/openclaw-npm-release.yml` 和构建 commit `723d9c20878e8ecadb684883854b9707d5c31050`。账本明确以审阅者证据记录两条来源，不从包版本猜 commit。

Node 官方签名 SHASUMS 的 `.tar.gz` 摘要为 `eb6728e77c576a35162c122c45ccadf224d89ff00abc8dae46cc2897c6002d7c`；`.tar.xz` 摘要为 `6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6`（本安装器选择 `.tar.gz`）。安装时仍须重新进行完整签名及摘要验证。V1 proof 的 `official_chain=unverified-v2` 字段按原契约保留；V2 的官方链验证发生在安装时，不改变 V1 门禁或声称它会在线复验。

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
