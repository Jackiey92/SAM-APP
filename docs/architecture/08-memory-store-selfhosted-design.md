# SAM Memory 记忆层自研设计（正式摘除 OpenViking）

> M4 状态（2026-10-12）：旧连接器/装配/服务探针已摘除，默认 localvector。新增 sam://kv/co/<company>/<domain>/... 与 sam://kv/rt/<session>/<task>/... 独立 JSON KV（不向量、不 embedding）；session 保持预留。正文与附录保留原始设计/比较证据，执行状态以 [本地记忆](../local-memory.md) 为准。依赖清单原本无旧后台服务包，M4 未卸载外部工具或清理物理数据。尚未做 M4 真机/独立断网/Gate。

- 状态：**v1.1**（范范 2026-10-11 批注修订：URI 改 sam:// 三区命名 original/facts/session；文件名禁下划线；模型与 embedding 可插拔、该用外部就用）
- 日期：2026-10-11
- 决策人：范范
- 前置：06（L1/L0 分层）、07（责任归属）、《SAM×OV 对接架构研究》及其探针修正
- 关系：**本文 supersede（取代）06 中对 OpenViking 的一切依赖**。06 的业务分层（L2 事实 / L1 简报 / L0 导览、固定模块树、数字可回查、产物署名）保持有效；变化的是**承载这些分层的底座从 OV 改为 SAM 自研薄层**。

### 修订记录
- v1.0：首版定稿（两份 OV 源码机制研究报告回填 §12.1 / §12.2）。
- v1.1（范范批注）：
  1. 员工层模型为暂定、可换；embedding/rerank 等**不锁死本地**，做成可插拔，该用外部模型时可显式引入；
  2. URI 根协议由 `viking://resources/sam/2b` 改为 `sam://`，三区平级命名：**original / facts / session**（原 2A/2B/2C 仅为历史方便叫法）；
  3. **所有文件/目录名禁止下划线**：guide.md / brief.md / 待归类 / 待办与冲突；落盘 data/facts。

---

## 0. 一句话结论

OV 的"通用文件压缩 + 抽样 + 软约束 + 自底向上摘要"数据管道与 SAM"先有归属过、可回查的事实，再精确汇总"的要求在**数据模型层错配**；其存储核心 21 万行、与 server/session/telemetry 等十余模块强耦合、向量引擎自研、AGPL + Alpha，**摘核心自维护（fork）不可行，骑缝缝合不可取**。
因此：**正式摘除 OV，自研一个只做三件事的薄记忆底座**——本地文件存储、向量检索、按固定模块树的分层装配；解析、L0/L1 内容生产、事实抽取、数字把关、署名全部沿用 SAM 自有员工链。
底座用**标准成熟积木**（SQLite + sqlite-vec + embedding 提供方可插拔，默认本地中文模型），SAM 只写最薄的业务胶水，不发明数据库、不背 Alpha 债；默认数据不出本机，外部模型作为显式可选项。

---

## 1. 设计目标与非目标

### 1.1 目标
1. **默认数据不出本机**：存储、检索、装配默认全部本地离线完成；embedding 等模型调用默认本地，外部提供方须显式配置授权后方可启用。
2. **事实为中心**：记忆原子是"归属过、可回查、有署名的事实卡"，不是"文件的通用摘要"。
3. **数字机械可回查**：L0/L1 中每个数字都能定位到具体事实卡，事实卡能定位到原件坐标；做不到就拒收，不靠模型自觉。
4. **形状固定、判断不写死**：固定模块文件夹树（对齐原型），但"材料归哪个模块"由员工判断，后端无关键词/正则归类。
5. **可重建、可替换**：向量索引和记忆文件都是 SQLite 正本（app.db）的**只读派生投影**，随时可从正本整体重建。
6. **业务层零感知换底座**：所有底座访问收口在 `MemoryProvider` 协议；自研底座实现同一协议，员工/服务/装配器代码不改。
7. **模型可插拔（v1.1）**：员工推理模型、embedding、rerank 均为可替换提供方，架构不绑定任何单一模型或单一部署形态。

### 1.2 非目标（本期明确不做）
- 不自研 ANN 索引算法、不自研数据库引擎（用 sqlite-vec）。
- 不做 OV 那套通用自动摘要、个人记忆分区（soul/experiences/cases 等）、多租户。
- 不做云端托管、多设备同步（线上=只读本地版，另案）。
- 不做"自动 L1 由系统压缩生成"；L1/L0 是员工产物。
- **session 区（sam://session/）本期只占名、不实现**：会话记忆另案设计，本期 M0–M6 不触碰。

---

## 2. 总体架构（分层与边界）

> 说明（v1.1）：员工层模型当前暂定 qwen 系列，**随时可换**；embedding 默认本地，但**不锁死本地**，可配置切换外部提供方（见 §6.2）。

```
┌─────────────────────────────────────────────────────────┐
│  员工层（推理模型暂定 qwen 系列，可换 + skill + tool）      │
│  document-classifier 归类 · 事实抽取 · L1 简报 · L0 导览    │
│  （业务判断只在这里发生；后端无任何归类/摘要规则）           │
└───────────────▲───────────────────┬─────────────────────┘
                │读：装配后的上下文     │写：事实卡/简报/导览(署名)
┌───────────────┴───────────────────▼─────────────────────┐
│  SAM 记忆服务层（自有，薄）                                │
│  · ContextAssembler 按固定模块树+token预算分层下钻          │
│  · 数字护栏 / 署名(戳) / 投影重建 / URI 契约                │
│  · L0GuideService / L1BriefService / CompanyFacts         │
└───────────────▲─────────────────────────────────────────┘
                │ 仅依赖协议，不依赖具体实现
┌───────────────┴─────────────────────────────────────────┐
│  MemoryProvider 协议（app/ports.py，已存在）               │
│  put / read / delete / search / query / ensure_directory  │
│  put_fact / get_fact / list_facts ...                     │
└───────────────▲─────────────────────────────────────────┘
                │ 实现
┌───────────────┴─────────────────────────────────────────┐
│  LocalVectorMemoryProvider（新，替代 OpenVikingMemoryProvider)│
│  ┌──────────────┐  ┌────────────────┐  ┌──────────────┐ │
│  │ 内容存储      │  │ 向量索引         │  │ 全文/前缀检索  │ │
│  │ SQLite 正本   │  │ sqlite-vec      │  │ FTS5 / LIKE  │ │
│  │ app.db        │  │ + 派生vec表     │  │              │ │
│  └──────────────┘  └────────────────┘  └──────────────┘ │
│  Embedding：可插拔提供方，默认本地 bge-small-zh             │
│  Chunking：固定块大小+重叠 + 保留来源/顺序元数据            │
└──────────────────────────────────────────────────────────┘
        ▲ 原件坐标(sam://original)
┌───────┴──────────────────┐
│ original 原料库（不进语义记忆）│  自有解析(docling/MinerU)，原件 bin + 映射 md
└──────────────────────────┘
```

三条铁边界：
- **判断**只在员工层；**存储/检索/装配/护栏**在服务层；**积木**在底座层。
- 正本只在 SQLite；记忆文件与向量是可重建投影。
- 底座只认"协议 + URI + 元数据"，不认识任何业务概念（不知道什么是"财务模块"）。

---

## 3. 数据模型

### 3.1 正本（SQLite app.db，权威）
沿用并补齐现有事实/待办表，新增/明确：
- `facts`：事实正本（fact_id、company_id、module_key、period、subject、claim、数值字段、source 坐标、attribution_id、status、content_hash、updated_at…）。
- `attributions`：责任账本（阶段4，attribution_id 权威来源：clerk/skill/model/run_id/source_sha256/created_at）。
- `source_files / parse_jobs / parse_staging / file_classifications`：original 原件与解析、归类（员工产出）。
- 记忆投影表（见 3.3）。

### 3.2 URI 契约（v1.1：sam:// 三区命名）

历史文档里的 2A/2B/2C 只是方便叫法，正式命名为三个平级语义区：

| 历史叫法 | 正式区名 | 含义 | 本期 |
|---|---|---|---|
| 2A | **original** | 原件库：原件 bin + 解析映射 md，不进语义记忆 | 实现 |
| 2B | **facts** | 事实记忆：L2 事实卡 / L1 简报 / L0 导览 | 实现 |
| 2C | **session** | 会话记忆 | **仅占名，不实现**（另案） |

根与层级：
- scheme 固定为 `sam://`（与 OV 彻底脱钩，协议中立、团队自有的内部逻辑寻址）；
- `facts_root   = sam://facts/<company_id>/<module_key>/`（某模块事实卡目录）；
- `company_root = sam://facts/<company_id>/`（一家公司的事实记忆根）；
- `original_root = sam://original/<company_id>/...`（原件及解析产物，由解析链决定内部布局）；
- `session_root = sam://session/<company_id>/...`（预留，本期不创建、不读写）。

规则：
- 所有 URI 拼接只在 `paths.py` 一处发生，做归一化校验；上层只传语义 id（company_id/module_key/fact_id），不传半拼好的 URI。
- URI 不 imply 任何具体落盘形态：落盘映射为 SQLite 行 + 可选导出为真实目录树（见 3.3）。
- 不再出现 `viking://`、`resources/`、`2a/2b/2c` 等任何历史段。

### 3.3 投影（派生、只读、可重建）
- **内容投影**：事实卡/简报/导览渲染成的 Markdown（人读模型读同一份），存表 + 可导出到 `data/facts/<company>/...` 真实目录（供人查看/打包）。原件导出到 `data/original/<company>/...`。
- **向量投影**：每个可检索单元（事实卡=1 单元；长简报/导览切块）一行：`chunk_id、uri、company_id、module_key、chunk_index、chunk_text、embedding(BLOB)、content_hash、attribution_id`。
- 写正本（事务提交）→ 渲染 md → 计算 hash → embedding → upsert 向量行；任一失败标记该公司投影 stale，不污染正本。重建 = 按正本全量重算。

### 3.4 三层记忆（沿用 06，全部普通 Markdown，文件名无下划线）
- **L2 事实卡** `<module_key>/F<fact_id>.md`：普通 md，YAML frontmatter 盖戳，只建向量不生成摘要。
- **L1 简报** `<module_key>/brief.md`（模块级）+ 公司根 `brief.md`（公司级）：员工从事实卡写，内联引用 `[来源:F12,F18]`，不新增任何数字。
- **L0 导览** `<module_key>/guide.md` + 公司根 `guide.md`：一页导航/结论。
- 固定模块树（见 §4）。

---

## 4. 固定模块树（写死结构，不写死归类；目录/文件名无下划线）

公司根：`sam://facts/<company_id>/`（导出形态 `data/facts/<company_id>/`）
```
guide.md  brief.md                            # 公司级 L0/L1
01-企业基本信息/  (guide.md brief.md F*.md)   # info
02-业务概览/                                   # bizov
03-人员管理/                                   # hr
04-生产/        〔暂隐〕                        # prod
05-采购/        〔暂隐〕                        # purch
06-销售/        〔暂隐〕                        # sales
07-市场/                                       # mkt
08-财务/        〔暂隐〕                        # fin
09-融资/        〔暂隐〕                        # fund
待办与冲突/                                    # todos & conflicts
待归类/                                        # 员工拿不准的落点，禁止后端猜
（原件不在本区：sam://original/<company_id>/，仅坐标引用，不参与语义检索）
```
- 颗粒度到此为止，不再下钻子文件夹。
- 模块清单（序号、目录名、module_key、原型 data-go、是否暂隐）是**固定 taxonomy 常量**，可写死。
- **命名铁律（v1.1）**：所有本系统产出的文件/目录名禁止下划线开头、禁止下划线分词；用中文短名或无分隔英文。历史代码里的 `_guide/_brief/_待归类/_待办与冲突` 一律按上表改名。
- **归类判断禁止写死**：不允许任何关键词/正则/别名/枚举映射把材料自动归模块；由 document-classifier 员工产出 module_key，拿不准进 `待归类/` 并产 unresolved 交接。
- 每个模块目录各有自己的 L0/L1；公司根有公司级 L0/L1。

---

## 5. MemoryProvider 协议（替换面，业务零改动）

现有 `app/ports.py::MemoryProvider` 已定义所需方法，自研底座实现同一协议：
- 内容：`put(uri, content, metadata)`、`read(uri)`、`delete(uri, recursive)`、`ensure_directory(uri)`
- 检索：`search(query, prefix) -> [{uri, score, snippet, module_key, company_id}]`、`query(prefix, query)`（前缀+可选文本）
- 事实便捷口：`put_fact / get_fact / list_facts`
- 资源（original）：`add_resource / add_resource_to / wait_for_resource`（自研版落本地原料库）
- 已有 `LocalMemoryProvider`（纯文件、无语义）作为离线/测试兜底；新 `LocalVectorMemoryProvider` 在其基础上加 SQLite+向量。
- 配置默认只允许 loopback / 纯本地；外部模型提供方必须经配置显式开启（见 §6.2）。

实施时删除/隔离 `OpenVikingMemoryProvider`，由 providers.py 装配切到本地产向量实现；保留协议口以便将来上云时再换实现（但业务仍不感知）。

---

## 6. 存储选型（标准积木，不发明轮子）

### 6.1 向量库：SQLite + sqlite-vec
- 选型：**sqlite-vec 0.1.9**（可加载进现有 SQLite 3.46 的扩展），理由：
  - 与正本同一 SQLite，零额外服务、单文件、事务/备份/快照天然一致；
  - 纯嵌入式、本地、无 AGPL（MIT）、无独立进程；
  - 数据量级（单公司事实卡数百到数千、切块万级）远在其舒适区；
  - 不自己维护索引算法（满足"不养数据库引擎"）。
- 备选（若 sqlite-vec 在 Python 3.14/召回质量上不达标）：usearch（同样嵌入式、Apache-2.0、自带 HNSW）。决策以 PoC 实测为准。
- 元数据过滤（company_id/module_key/status）先走 SQL 预筛再做向量排序，或存冗余列 SQL 过滤；不依赖向量库的复杂 filter DSL。

### 6.2 Embedding：可插拔提供方，默认本地（v1.1 修订）

**原则：不死磕纯本地，该用外部模型就引入；但默认本地、外部必须显式配置授权。**

- **默认提供方：本地 BAAI/bge-small-zh-v1.5**（512 维，约 100MB，CPU 可跑，中文检索强），用已在依赖中的 transformers + torch(CPU) 本地推理；首次一次性下载模型后 `HF_HUB_OFFLINE=1` 纯离线，模型随部署预装，禁止运行时静默联网。
- **提供方抽象 `EmbeddingProvider`（协议化）**：统一 `embed_texts(texts) / embed_query(text)`，至少两个实现：
  - `LocalBgeProvider`（默认）：本地 bge-small-zh；
  - `RemoteOpenAICompatibleProvider`：OpenAI 兼容端点（豆包/其他国内端点/OpenAI），endpoint 与 key 走配置，**默认禁用**，启用需显式配置项（如 `SAM_EMBEDDING_PROVIDER=remote`）并在启动日志明示"记忆向量化数据将出域至 <endpoint>"。
  - 同一时刻全库只允许一个提供方+一个模型版本；提供方/版本写入元数据，切换即整体重建向量投影。
- query 侧按 bge 要求加指令前缀；外部模型按其官方指令处理。
- 升级路线：bge-base-zh、外部大模型 embedding、onnx/量化，均为换实现，不动架构。PoC 用中文财务语句实测召回后定默认。
- rerank 同例（见 §7.1）：默认无，可配置本地交叉编码器或外部 rerank 端点。

### 6.3 切块（chunking）策略（自研，规格）
- 默认单元：**事实卡不切**（一张卡一个向量，语义自洽、天然带来源署名）。
- 长文本（公司/模块 L1、L0 超长时、或将来的解析文本）按约 500–800 汉字一块、相邻重叠约 80–120 字，尽量在段落/标题边界切，**绝不从数字/链接中间截断**。
- 每块元数据：`uri、company_id、module_key、chunk_index、chunk_total、source_span(可选)、attribution_id、content_hash、embed_model_version`。
- 表格：按行组保留表头上下文（表头随块携带），避免数字错列。

---

## 7. 检索与分层装配（SAM 自有，替代 OV context_assembler）

> v1.1：外部模型（query 改写、rerank 等）**不必为了"纯血本地"而弃用**——默认走零外部依赖的最小可靠版，效果不够时按 §6.2 的可插拔方式显式引入。**本系统产出的所有文件/目录名禁止下划线。**

### 7.1 search 语义检索
1. query embedding（提供方按 §6.2 配置，默认本地）；
2. 按 `prefix`/company_id/module_key/status 做 SQL 范围预筛（"限根"在我们这里是真实的 SQL 过滤，物理上不可能跨公司/跨根泄漏——顺带根治 OV 双实例越权风险）；
3. sqlite-vec KNN 取 top_k（默认 k=20–30）余弦相似度；
4. 返回 uri/score/片段/模块/事实 id；
5. **可选增强（默认关，显式配置启用）**：LLM query 改写、外部/本地 rerank。rerank 提供方同样协议化（本地 bge-reranker 或外部端点），出域时启动日志明示。

### 7.2 ContextAssembler 分层下钻（沿用并强化现有 app/context_assembler.py 雏形）
喂给业务员工/经理的常驻上下文，按固定顺序与 token 预算装配：
1. 公司 `guide.md`（L0，全局导航+结论）；
2. 与问题相关模块：先模块 `guide.md`，再模块 `brief.md`（L1）；
3. 用 search 在该公司范围内选相关事实卡（L2），按相关度+模块归属取；
4. 需要原件时只给 **sam://original 坐标**（不灌原件全文），由员工显式调取；
5. 全程受 `SAM_CTX_RESIDENT_TOKEN_BUDGET`（默认 6000）约束，超预算按"整块折叠成指针"裁剪，只丢整块、不腰斩数字/链接（现有行为保留）。
- 与 OV 的本质区别：我们是"**用问题去事实卡里取、再经员工产出的 L1/L0 组织**"，自顶向下、事实可回查；不是"把文件层层压缩成摘要"。

### 7.3 数字机械护栏
- L1/L0 渲染/入库前跑确定性校验（无 LLM）：抽取正文中所有数字 token，每个必须能在其引用的事实卡中逐字找到，且期间/主体/口径一致；找不到或对不上 → **拒收并产 unresolved**，不允许进入记忆层。
- 事实卡数值以结构化字段为准，正文数字由字段生成，避免"解析值/公式值"污染（呼应解析结论：公式不取解析文本值，取 data_only/重算后值）。

---

## 8. 产物署名（沿用 07，三层统一 frontmatter）
- 三层全是普通 md（guide.md / brief.md / F*.md），统一 YAML frontmatter 盖戳：attribution_id（阶段3占位，阶段4回填）+ clerk/skill/model/source_sha256 等人读冗余。
- 独立 `attributions` 账本为权威；先落账后盖戳、无戳即无账、无账不入记忆层。
- 向量投影携带 attribution_id，检索结果可追溯到产出来源。

---

## 9. 隐私、出域与隔离
- **默认全链路本地**：存储 SQLite、embedding 默认本地 bge、检索本地；无任何默认外部 API key。
- **外部模型是显式例外（v1.1）**：启用 remote embedding/rerank/query 改写必须有配置项+授权，启动日志明示数据流向与端点；未配置即不可能出域。客户敏感场景保持默认本地档。
- 解析：自研（docling/MinerU/original 链），**不使用 firecrawl**；hosted OCR 永不启用（除非未来显式另案授权）。
- "限根"由 SQL 过滤物理保证；公司间数据行级隔离，杜绝跨公司召回。
- 不再有"双实例/配置回退连到云端个人记忆"的风险面——没有任何隐式云端组件。
- original 原件默认不出本机；线上只读窗口只收快照、写操作 403（沿用现状）。

---

## 10. 迁移与摘除路径（工程切分，代码全部派 CRIS）
> 饭团只做设计/交接/验收；以下每步一份 docs/handoffs 交接单（开头不写身份），CRIS 看本设计+交接单实现，**不看 OV 源码**（洁净室）。

- **M0 PoC（小、先证伪）**：空仓库里验证 sqlite-vec 在 py3.14 + SQLite3.46 装得上、建库/增删/查/余弦 top-k 正常；`EmbeddingProvider` 抽象搭好，本地 bge 加载、离线推理、中文财务语句召回质量达标；给结论与基准（QPS、召回、磁盘）。
- **M1 LocalVectorMemoryProvider**：实现 ports 协议；写正本→渲染md（无下划线命名）→切块→embedding→向量 upsert；delete/list/search/query；投影可重建；URI 全部走 sam:// + paths.py 单点收口。
- **M2 装配切换**：providers 装配切到新 provider；ContextAssembler/L0/L1 服务接新 search（guide.md/brief.md 新文件名）；全库下划线文件名改名清干净。
- **M3 数字护栏 + 失败终态**：L1/L0 入库数字回查校验；webapp 不再吞异常，重建失败 job 标失败终态。
- **M4 摘除 OV**：删除/停用 OpenVikingMemoryProvider、ensure-local-ov、1933 依赖与相关配置；requirements 去掉 openviking；清 OV 数据通道；保留 original 自研解析。
- **M5 验收**：622+ 测试改造全绿；真机端到端连跑两遍 status=success；高保真原型视觉零变化；测完即清（仅留"谷斗科技（上海）有限公司"）；默认档断网再跑一遍证明全本地。
- **M6 提交/部署**：commit/push；重建 staging（startupaimanager01.coze.site）只读快照。

每步可独立回滚；M1–M2 期间允许 LocalMemoryProvider 兜底。

---

## 11. 风险与对策
- **召回质量不足**：PoC 用真实中文底稿做召回基线；可调 k、升级 bge-base、启用外部 embedding/rerank（§6.2 已留口子），不影响架构。
- **sqlite-vec 成熟度/上限**：数据量小、可重建，风险低；备选 usearch；最坏换向量库也只改底座一个实现，业务不动。
- **外部模型出域合规**：默认关闭、配置显式开启+日志明示；部署档分"本地档/增强档"，客户场景默认本地档。
- **首次模型下载/打包体积**：预置模型、离线安装；CPU 推理延迟实测，必要时 onnx/量化；嫌重直接切外部提供方。
- **维护责任**：我们只维护"薄胶水 + 选定积木"，不维护数据库引擎；sqlite-vec/bge 是活跃主流项目，风险显著低于 fork OV。
- **法律**：无 OV 代码复制、无 import、无 AGPL 传染；设计文档为洁净室规格。商用前法务确认借鉴边界（思想不受保护）。

---

## 12. OV 机制规格对照（洁净室产出，不含代码）
> 来源：两个只读研究 Agent 通读 OV 源码后产出的机制规格与默认参数（不抄代码）。本节列出 OV 的**行为契约与默认值**，作为我们自研实现的"功能对照表"——逐条决定"保留 / 简化 / 砍掉 / 用成熟件替代"，确认没有漏掉关键行为。

### 12.1 写入 / 切块 / embedding / 向量存储

#### 12.1.1 写入入库链

OV 行为（同步落盘 + 三级异步队列 + 语义 DAG）：
1. 文件同步写入虚拟文件系统（VikingFS/AGFS，实际落本地磁盘）。
2. 触发 Semantic 队列消息，执行**自底向上 DAG**：每个文件 → LLM 生成 L2 摘要；每个目录 → 聚合子节点 → LLM 生成 L0 abstract + L1 overview；摘要落 sidecar 文件。
3. 每个节点摘要完成即投递 Embedding 队列，消费者调 embedder 生成向量 → upsert 到 VectorDB。
4. **幂等 / 去重完全靠确定性 ID**：`record_id = md5(account_id:seed_uri)`；同 URI 重复写入自然覆盖（upsert）。

OV 关键默认值：批量最大操作数 256、单文件 8MB、总 16MB；abstract 字节上限 50KB；允许扩展名白名单（.md/.txt/.json/.yaml/.yml/.toml/.py/.js/.ts）。

OV 边界：空文件/二进制文件跳过向量化不报错；abstract 超 50KB 被截断（影响向量质量）；并发同 URI 靠 pathlock 串行化；队列有 reenqueue 重试。

**我们的取舍**：
- **采纳**：确定性 ID = md5(uri) 作为向量主键，天然幂等、同 URI upsert 覆盖（简化为 md5(uri)，去掉 account_id 段——我们单租户）。
- **保留**：三层语义（L0/L1/L2）；异步写入队列解耦（但用 **SQLite WAL + 简单 worker** 替代三级队列，摘要/向量只是正本派生投影）。
- **砍掉**：VikingFS/AGFS（直接用 sam:// 寻址 + SQLite 正本 + 本地文件系统导出）；sidecar 文件（摘要/向量全在数据库投影里，不污染 md 可读层）；多租户 ACL；扩展名白名单（我们是事实卡 + 简报 + 导览，格式固定）。
- 队列用 SQLite 事务 + 单 writer，天然串行化，不引入额外组件。

#### 12.1.2 切块策略

OV 行为：**不做传统文本切分**，"切块"就是三级语义层次：
- L2：一个文件 = 一个 chunk（超 4096 token 截断到前缀，追 "...(truncated for embedding)"）；
- L1：一个目录 = 一个 overview chunk；
- L0：一个目录 = 一个 abstract chunk。
- Token 估算：CJK 字符 1:1，其余字符 1:4；二分查找最大可嵌入前缀。
- 元数据只记 URI + level + abstract，无块内位置、段落索引。
- 默认 text_source=content_only（嵌入全文截断），可选 summary_first（嵌入 LLM 摘要）。

OV 关键默认值：max_input_tokens=4096；abstract 字节上限 50KB；目录 overview 采样子节点数 32。

OV 边界：没有段落级检索粒度；CJK token 估算偏保守；表格/代码无特殊处理；每块只记 URI+level+abstract。

**我们的取舍**：
- **保留**：事实卡作为一个 chunk（每张卡 200-800 字，天然在 4096 token 内，无需切分）；L0/L1 各作为一个 chunk。
- **采纳**：CJK token 1:1 估算（中文准确度高于"字符/4"）；二分查找截断保留作为兜底。
- **增强**：长文本（解析产物、将来自定义长 L0/L1）按 Markdown 标题（`#`/`##`）做**语义切分**，每块带 source_span 元数据，支持段落级定位——这是 OV 缺的，也是我们事实可回查需要的。
- 表格按行组保留表头上下文。

#### 12.1.3 Embedding 链

OV 行为：
- **默认纯本地离线**：provider=local, model=bge-small-zh-v1.5-f16（GGUF 格式，~100MB），通过 llama-cpp-python 加载，首次从 HuggingFace 下载后纯本地推理；维度 512。
- **Query 端加不对称前缀**：`"为这个句子生成表示以用于检索相关文章："`（BGE 标准做法）；文档端不加。
- 工厂支持 13 种 provider，多凭证 failover + 断路器。
- 批量 batch_size=32，max_retries=3，max_concurrent=10；failback 超时 600s/50 次请求。

OV 关键默认值：model=bge-small-zh-v1.5-f16、dimension=512、batch_size=32、缓存路径 `~/.cache/openviking/models/bge-small-zh-v1.5-f16.gguf`。

OV 边界：首次需联网下载模型；维度固定 512；本地 embedder 单条处理无 batch；query/document 不对称前缀是硬编码。

**我们的取舍（v1.1）**：
- **默认本地 bge-small-zh-v1.5 纯离线**——与 OV 默认选型一致，印证这是业界标准选择；query 端不对称前缀。
- **但不锁死**：`EmbeddingProvider` 协议化，本地 bge 为默认实现，OpenAI 兼容外部端点为可选实现（显式配置启用）；13 种 provider 工厂、多凭证 failover、断路器全部砍掉。
- 用已经在依赖里的 `transformers + torch(CPU)` 加载 PyTorch 格式模型，不引入 llama-cpp-python；模型缓存随部署预装，本地档启动强制离线。
- **保留**：batch_size=32、max_retries=3；向量维度 512 与模型版本写入元数据，换模型/换提供方整体重建索引。
- PoC 阶段校准 CJK token 估算系数。

#### 12.1.4 向量存储

OV 行为（自研引擎）：
- 自研 C++ HNSW 实现，SIMD（SSE3/AVX2/AVX512）自动选择；标量存储用 RocksDB（32MB 页大小）。
- 单一 collection "context"，所有账户/层级共享；标量索引字段：uri/type/context_type/created_at/updated_at/level/name/tags/search_tags/account_id/owner_user_id/acl_mode。
- 距离度量余弦；sparse_weight=0（默认不做稀疏混合）。
- Upsert 流程：校验维度一致性 → 过滤 schema 外字段 → 绑定 account_id → 写 RocksDB + 更新 HNSW 索引。
- 删除语义：DeltaRecord tombstone 标记删除，**后台定期 rebuild 回收空间**（HNSW 不支持动态删除）。
- 检索：构造过滤表达式 → HNSW ANN top-k → 标量过滤 **post-filter**。
- 并发：读写锁（写串行、读并发）。

OV 边界：维度必须一致（否则 upsert 报错）；单 collection 大规模需拆分；删除是标记删除需 rebuild；content 字段本地后端不存（仅 VikingDB 后端用 BM25）。

**我们的取舍**：
- **不采 OV 的自研引擎路线**——这正是选 sqlite-vec 的根本理由：OV 自研 = 我们要养一个数据库；sqlite-vec = 用成熟件，维护成本降到 SQLite 社区水平。
- **保留的设计思想**：确定性 ID 主键（已采纳）；标量过滤 + ANN top-k 的检索模式（sqlite-vec 同样支持 SQL 预筛 + KNN）；余弦距离（采纳）。
- **增强**：SQLite 单文件与正本同库，备份/恢复/快照天然一致；删除直接 SQL DELETE，sqlite-vec 同步清理（不需要 rebuild）；事务保证正本/向量强一致性（OV 是最终一致性）。
- 备选 usearch（嵌入式 HNSW，Apache-2.0）作为 sqlite-vec 不达标时的 fallback；决策以 PoC 实测为准。
- 单表（按 company_id/module_key/level 列 SQL 过滤），与 OV 同思路；大规模再拆分。

### 12.2 检索 / 分层装配 / 关系

#### 12.2.1 语义检索链

OV 行为（三阶流水线）：
1. **Query 改写（默认开）**：用 LLM 把原问题扩展成最多 3 个变体，每个含意图/优先级；原 query 永远在第一位。5 秒超时，失败则降级为原 query。
2. **向量召回**：每个变体独立 embedding → 调向量库 ANN 取 `用户 limit × 2` 候选；多 query 结果按 URI 去重、保留最高分；可选 level/目录/context_type/extra_filter 过滤；余弦相似度映射到 [0,1]；可选混合 dense+sparse 按 alpha 融合。
3. **Rerank（默认关，mode=thinking 时开）**：候选用各自 abstract 做文档，query 截断到 max_input_tokens×3/4，调 rerank 服务返回新分数，阈值 0.1 过滤，取 top-limit。失败降级回向量分数。
4. **结果转换**：每个命中附加层级后缀，提取正文。

OV 关键默认值：`default_search_limit=10`, `RERANK_CANDIDATE_MULTIPLIER=2`, `MAX_PLANNED_QUERIES=3`, `rerank_threshold=0.1`, `recall_dedup_turns=5`。

OV 边界：query 改写 JSON 不稳定；rerank 额外 100–500ms；cosine/IP/rerank 分数尺度不同。

**我们的取舍（v1.1）**：
- 默认最小版：单 query 召回（SAM 场景固定），不做改写、不开 rerank；保留 URI 前缀范围物理限根、level 过滤、多结果去重取最高分。
- **外部增强不排斥**：query 改写、rerank 作为可插拔可选项（本地模型或外部端点，显式配置启用、出域明示）；不再写"将来也只用本地"的死限制。
- 多 provider、时间衰减、跨 peer 惩罚砍掉。
- 向量库用 sqlite-vec（已选型），不随报告建议改 Qdrant/Chroma/Milvus——数据量级不需要独立服务、单文件与正本同库更方便备份重建。

#### 12.2.2 分层上下文装配（**最值得保留的 OV 设计**）

OV 行为（**广度优先 + 深度升级**）：
1. 收集候选：按类别（OV 用 events/entities/preferences/...）分组，每组独立召回，每组按配额截取，合并去重；来源优先级 actor_peer > self > other_peer；`ranked_score = score - penalty`。
2. 内容预取：需要 overview 或 full 的 URI 并发读，信号量限 8；记忆文件提取 content 字段，其他读原文。
3. **预算分配核心**：
   - **广度优先**：所有候选按分数排序；每条先放默认层级（events=overview、其他=abstract）；per_entry_cap = `max_tokens / candidate_count × 2`（单条最多占平均份额 2 倍）；按层级从默认到 uri 逐级尝试，能放下就定。
   - **深度升级**：三轮贪心升级——第一轮尝试 overview、第二轮尝试 full、第三轮放宽 per_entry_cap 再尝试 full；每轮内按分数排序，delta token 在剩余预算内即升级。
   - **层级降级**：abstract 为空时按类别处理（记忆类降级到 overview、资源/技能类降级到 uri 不读文件）；目录类型强制 overview（禁止 full = 整子树）。
4. 渲染：每个 entry 一个 XML tag（uri/type/score/detail），body 防伪造转义。
5. 可选摘要重写：超时/失败不影响主输出。

OV 关键默认值：`DEFAULT_MAX_TOKENS=1600`, `DEFAULT_TIER=abstract`, `SEPARATOR_TOKENS=1`, `READ_CONCURRENCY=8`, `MAX_EXCLUDE_URIS=200`。

OV 边界：token 估算用字符/4、中英混合误差大；升级贪心非全局最优；per_entry_cap 可能拦大文件；目录禁 full；记忆类的 abstract 实际是全文。

**我们的取舍**：
- **保留核心思想**：分层渐进展开 + token 预算 + "广度优先 + 深度升级"的贪心分配策略。
- 简化类别系统：我们固定模块树 + 公司根 + 待办与冲突/待归类，没有 OV 的 8 类记忆类型；每模块默认 tier 由装配器固定（模块 guide.md=L0、brief.md=L1、事实卡=L2），不做动态配额。
- 砍：跨 peer 惩罚、跨轮次去重 ledger、摘要重写、purpose 预设配额。
- 改 token 估算：CJK 1:1 起步（采纳 §12.1.2），PoC 用中文语料校准，必要时按实际词表数；**SAM 默认预算 6000 token（沿用现状，比 OV 默认 1600 更合业务场景）**。
- 渲染保持现有 `app/context_assembler.py` 的 Markdown 风格（非 OV XML 标签），人/模型可读一致。
- 层级降级：我们不需要——L0/L1 是员工产物不会空、事实卡全文即 L2。

#### 12.2.3 关系元数据与目录

OV 行为：
- 目录树枚举：递归 ls、过滤隐藏文件（sidecar/relations 等）。
- `.relations.json` 是**保留文件名**、存关系元数据，但**不参与向量检索**；实际关系靠 URI 层级 + 记忆 frontmatter 的 links/backlinks。
- **新鲜度（Freshness）机制**：子文件变更累计 pending，超过 `refresh_ratio`（默认 0.1）或无 baseline 时触发父目录 L0/L1 重算；目录条目数 ≤ `overview_sample_limit` 时总是重算；否则只标记 pending。
- OKF 文件格式：YAML frontmatter（directory/source/generated_by/freshness）+ body；directory 必填；解析失败即报错。
- Overview 生成策略：目录读 overview body；代码文件抽 AST 骨架；记忆文件抽 Summary 节；普通文档抽标题树+第一段（400 字符）。

OV 关键默认值：`OVERVIEW_HEADING_LIMIT=24`, `OVERVIEW_PARAGRAPH_CHARS=400`, `refresh_ratio=0.1`。

**我们的取舍**：
- 砍 `.relations.json`（我们的关系是固定业务结构）、砍记忆链接/反向链接、砍 AST 骨架提取（我们不处理代码仓库）。
- 保留简化版新鲜度：模块/公司 L0/L1 在子事实卡入库时标 stale；**不做"变更比例阈值"**，因为 SAM 的 L0/L1 是员工产物、不是系统自动生成——员工主动触发重建，不存在"自动根据变更比例决定重算"的需求。
- OV 的 OKF 文件名/格式不用；L0=guide.md、L1=brief.md 是我们自己的无下划线命名，frontmatter 统一为产物署名戳。
