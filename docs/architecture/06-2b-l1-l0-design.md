# 2B 记忆层设计（L2 事实树 / L1 简报 / L0 导览）v2

> 历史设计：记忆底座实现已被 [08 自研设计](08-memory-store-selfhosted-design.md) 替代。M4（2026-10-12）代码摘除完成；本文旧运输、地址与启动描述仅供回溯，不是当前操作指南。当前 SQLite + sqlite-vec + 离线 BGE，以及独立 JSON KV，见 [本地记忆](../local-memory.md)。业务分层与来源约束仍保留。

> 版本：v2，2026-10-11 定稿；同日探针后修订为「路线乙·不占座」。取代 v1 中"双文件 md+json"方案。
> 承接 `02-memory-system.md`、`05-memory-code-layout.md`、`07-accountability.md`；
> 地基依据见《SAM×OV对接架构研究.md》。
> 最高约束：**业务判断全在文档管理员工（模型+技能+工具），后端无任何关键词/正则/枚举/写死公式；后端只做护栏与通用存储能力。**

---

## 0. v2 相对 v1 的根本变化（为什么改）

1. **撤掉全部业务 JSON**。对齐 OV（火山 ContextDB）官方范式：L0/L1 始终是 Markdown，人读与模型读同一份，靠 L0→L1→L2 渐进展开，不设"一份给人、一份给机器"。
2. **不占座（探针后定稿）**：员工写的 L0/L1 是**普通 md**（`_guide.md`/`_brief.md`），不写进 OV 保留的 `.abstract.md`/`.overview.md`；OV 只做存储 + 向量语义搜索，分层装配由 SAM 自研、按固定模块树路径取。OV 自带的 flash 通用摘要永不进经理上下文（写入统一 `vectors_only`，且 SAM 不读它的 sidecar）。早期"偷梁换柱占座"方案因 L1 无零-flash 安全创建门、座位会被重算盖回而放弃（见 4.1）。
3. **2B 是一棵对齐主页的固定模块树**，不是扁平事实堆。
4. 正本事实仍只存 SQLite（`facts` 等表）；OV 内一切 md 都是**可随时从正本重建的投影**，不是真相源。

---

## 1. 两套"L0/L1"撞名，必须分清

| | 生成者 | 内容性质 | 是否喂经理 |
|---|---|---|---|
| **OV 原生 L0/L1** | 内置 VLM（qwen3.8-flash）自底向上 | 文件夹的**通用**摘要，不懂主体归属、不保证数字 | 永不（关闭/忽略） |
| **SAM 业务 L2/L1/L0** | 文档管理员工（qwen3.8-max + 技能） | 归属过、数字可回查、冲突标待办的**公司**事实体系 | 只用这套 |

OV 负责"存、找、装多少"（存储/向量/解析/分层装配）；SAM 员工负责"内容对不对"。

---

## 2. 2B 固定模块树（1:1 对齐高保真原型，不下钻子文件夹）

主页原型共 9 大业务模块（其中多个当前 `deferred` 暂隐）。2B 树照此预建，**隐藏模块也预留空目录**（空目录即精致空态），颗粒度到此为止，模块下不再造子文件夹。

```
viking://resources/sam/2b/<company>/
    _guide.md           # 公司级 L0：极简导览，常驻 LEADER 上下文（普通 md）
    _brief.md           # 公司级 L1：公司简报（跨模块聚合，普通 md）
    01-企业基本信息/      _guide.md _brief.md  +  F<fact_id>.md …
    02-业务概览/          _guide.md _brief.md  +  F*.md
    03-人员管理/          _guide.md _brief.md  +  F*.md
    04-生产/  〔暂隐·空抽屉〕
    05-采购/  〔暂隐·空抽屉〕
    06-销售/  〔暂隐〕（销售台账事实落此）
    07-市场/
    08-财务/  〔暂隐〕
    09-融资/  〔暂隐〕
    _待办与冲突/          # 无主体裸数字、数字打架、跨模块冲突（系统抽屉，不映射主页）
    _待归类/              # 员工拿不准归属的事实卡，不硬塞（系统抽屉）
```

> 注：OV 在各目录还会自动生成自己的 `.abstract.md/.overview.md`，那是 OV 的通用座位，**SAM 不读、不写、不依赖**；SAM 业务三层只用上面的普通 md（`_guide.md/_brief.md/F*.md`）。

- 目录名带固定序号 + 模块名，**与原型导航/data-go 一一对应**（info/bizov/hr/prod/purch/sales/mkt/fin/fund），后端据此把模块注册到主页对应板块。
- 每个模块文件夹各有自己的 L0/L1（模块级摘要）；公司根的 L1 由员工从各模块 L1 聚合，公司根 L0 再从公司 L1 压缩。
- **模块树是"信息架构"，允许写死，但只能以一份可编辑的 taxonomy 清单存在（技能/配置数据），后端代码里没有任何归类 if/正则。** 加栏目=改清单，不改代码。
- 归类动作（某张事实卡放哪个模块）由 document-classifier 技能的员工判断；拿不准进 `_待归类`。
- 空模块不造事实，可不生成 L0/L1 普通 md（或仅留标题级占位），前端保持精致空态。

---

## 3. 三层产物的文件契约（全 Markdown）

### L2 —— 事实卡（模块目录下的普通 md）
- 路径：`<模块>/F<fact_id>.md`，文件名里的 fact_id 来自 SQLite 正本，重建幂等。
- 正文：一页纸事实——主体 / 属性 / 期间 / 取值+单位 / 置信状态 / 出处（原件 sha256、页码、locator 坐标）。
- 顶部用 **YAML frontmatter** 盖署名戳（普通 md 的 frontmatter 经源码确认被 OV 原样保留）；本阶段只放可空 `attribution_id` 占位，阶段4回填，见 07。
- 写入用 `ov write --processing-mode vectors_only`：**只建向量、不触发 flash 摘要**，且不引起父目录座位重算。

### L1 —— 公司/模块简报（普通 md `_brief.md`）
- 只能由员工从 SQLite 正本 + 各事实卡生成；**严禁回读旧 md 再加工**（避免副本套副本失真）。
- 每条结论后内联回查标注，如 `[来源 F12, F18]`；**不引入任何 L2 之外的数字**。
- 用 **YAML frontmatter** 盖署名戳（普通 md frontmatter 被 OV 原样保留），本阶段放可空 `attribution_id` 占位。
- 篇幅克制（对齐 OV L1 一两千字预算），避免被自研装配器截断。

### L0 —— 公司/模块导览（普通 md `_guide.md`）
- 员工 fact-guide 技能从对应 L1 压缩，只放入口级结论 + 下钻指引；不新增事实/数字。
- 公司根 L0 是常驻 LEADER 上下文的唯一业务层。
- 同样用普通 md frontmatter 署名。

> 不使用 OV 保留的 `.abstract.md/.overview.md`：那两个文件名由 OV 强制其 frontmatter 且会被语义重算盖回（见 4.1）。SAM 的 L0/L1 一律用 `_guide.md/_brief.md` 普通文件，完全自控。

---

## 4. OV 机制、源码证据与探针修正

不占座方案依赖以下已在本地 OV 0.4.23 证实的能力：

1. **关闭 flash 有官方开关**：`processing_mode=vectors_only`（`resource/processing_mode.py`）。该模式下 `should_summarize=False`，父目录刷新被 `not vectors_only` 拦截（`utils/resource_processor.py` 1138/1156 行），但仍 `_vectorize_resource_files` 建向量。→ 普通 md 写入只建向量、零 flash。
2. **建目录零-flash**：`ov mkdir <dir> --description` → `FSService.mkdir` 只用该文本生成 OV 自己的 `.abstract.md`，只 embed、不调 VLM（`service/fs_service.py` 380-430 行）。该 sidecar 我们不读不依赖，仅借 mkdir 建目录。
3. **公共写 API 禁止凭空创建 sidecar**（`storage/content_write.py:277-281`）——这是放弃占座 L1 的直接原因之一。

### 4.1 探针（2026-10-11）修正结论 —— 偷梁换柱改为「不占座」方案

真机探针（`docs/handoffs/` + 根目录 findings）推翻了早期两个乐观判断：

- **(P1 未闭环) `.overview.md` 没有零-flash 安全创建门**：公共 write 禁止凭空建；`cp` 虽能建但会排队语义刷新、改写成 flash 内容（`semantic_status=queued`，observer 见 flash Calls +1）；正规 OVPack 导入在 **dev 免密模式下被拒**（需 api_key 鉴权）；"关 VLM 后 reindex 落占位再替换"未真机验证，且需受控重启本地 OV。
- **(座位会被盖回)** 早期引用的"sidecar 已存在非空即沿用"只在 **skill 根**成立；**普通公司目录**上 `cp / mv / rm(父级刷新) / reindex` 都会自底向上重算并覆盖员工正文。占座不是一劳永逸，需长期防覆盖守卫。
- **(P2 通过)** 普通 md 以 `vectors_only` 写入：秒落盘、零 flash、`find` 命中 level=2、`read` 得全文；context 装配须**显式 detail=full / 精确 read** 才拿正文，默认 abstract 层只给 URI。事实卡树地基稳。
- **(P0 数据隔离澄清)** 本地 1933（dev）只装 `viking://resources/sam`，**不含** `viking://user/default` 个人记忆；饭团个人记忆在**云端** `api.vikingdb.cn-beijing.volces.com`（另一把 api_key）。本地关/配置 flash **不影响个人记忆**。

**据此定稿采用「路线乙：不占座」**（见下节），放弃把员工内容写进 `.abstract.md/.overview.md`。

### 4.2 路线乙定稿：三层全部普通 md，不碰 sidecar

OV 只承担「智能文件柜 + 语义搜索（向量）」，分层装配由 SAM 自研、按固定模块树路径取，不依赖 OV 原生 L0/L1 座位与自动装箱：

- **L2 事实卡**：`<模块>/F<fact_id>.md`，普通 md，`vectors_only` 写入，frontmatter 署名。
- **L1 简报**：普通 md `_brief.md`（公司根一份、每个模块一份）；公司根 = 各模块 L1 聚合，模块 L1 = 该模块事实卡聚合。
- **L0 导览**：普通 md `_guide.md`（公司根一份、每个模块一份），由对应 L1 压缩；公司根 `_guide.md` 常驻 LEADER。
- 建模块目录用 `mkdir --description` 放一句标题级说明（无害，只生成 OV 自己的 L0，SAM 不读它、也不被它影响）；SAM 只读自己的 `_guide.md/_brief.md/F*.md`。
- 读取/装配：按上述固定路径 `ov read`，喂经理时显式取全文（探针证实 detail=full/精确 read 可靠）；不靠 `ov find` 来组装 L0/L1，find 只用于"按问题找相关事实卡"。
- 因从不写 sidecar，**不存在被 flash 覆盖的问题**；`rm/cp/mv/reindex` 的语义副作用与 SAM 业务文件无关（普通 md 不被重算，仅随自身写入重 embed）。
- 下钻：公司 `_guide` → 模块 `_brief` → 模块 `F*.md` → 2A 原件坐标，路径固定、可逐级展开。
- 代价：不复用 OV 原生分层装箱器；自研装配按固定 9 模块树做 token 预算裁剪（现有 context_assembler 演进），结构固定反而更可控。

---

## 5. 员工与技能（沿用，一个员工）

唯一员工 `document_manager`，一套 identity/soul/memory；按工序现场加载技能：

| 技能 | 工序 | 产物 |
|---|---|---|
| document-classifier | 读 2A_md，判主体归属、归类到 9 模块/待归类 | 归类决定、L2 事实卡 |
| finance-fact-extraction | 提事实、标坐标、判存疑 | L2 事实卡 / 待办 |
| fact-brief | 从正本+事实卡聚合模块 L1 与公司 L1 | `_brief.md` |
| fact-guide | 从 L1 压缩模块 L0 与公司 L0 | `_guide.md` |

- 给不出结论 → 不猜不丢，产 unresolved/conflict 待办（原文+坐标+原因 handoff），进 `_待办与冲突`。
- 员工/经理 runId、模型、provider 等本阶段能拿到就透传到 `attribution_id` 占位，拿不到留空、不伪造；阶段4建独立责任账本（07）。

---

## 6. 后端职责（只护栏，无业务规则）

- 按 taxonomy 清单 `mkdir` 模块树、写 `_guide.md/_brief.md/F*.md`、重建/幂等、URI、并发锁、传输与超时。分清 product_root / company_root / facts_root 三层契约，禁止公司段重复（探针 P4）。
- 写入统一 `vectors_only`；投影写不再 `--wait` flash（阶段3事故根因），瞬时失败有限退避重试。
- **数字护栏（机械比对，不做判断）**：L1/L0 出现的每个数字字面量，必须能在其 `[来源 Fxx]` 引用的事实卡里找到；找不到则拒收该层并记错误/待办，绝不替员工补来源或改写数字。
- L1/L0 一律从 SQLite 正本重建；md 可删可重建。
- 失败终态要如实上报到 parse_job，不许吞异常仍标 done。

---

## 7. 验收

1. compileall + 全量 pytest 全绿（当前基线 622 passed/3 skipped，只增不减）。
2. **探针已完成并留存结论**（4.1 / docs/handoffs）：L2 普通 md + vectors_only 通过；L1 占座门未闭环 → 已定路线乙。
3. 真机：上传台账 → 固定模块树与 `_guide/_brief/F*` 正确生成；事实卡在对应模块、可 `ov find` 命中、按固定路径分层下钻；L1 数字全部可回查 L2，噪声 987.65/432.10 进 `_待办与冲突`；LEADER 问答含正确结论、不含未支撑数字。
4. 断网/关闭 VLM 条件下主流程仍成立（不依赖 flash）。
5. 界面视觉零变化（壳不动），空模块精致空态；测完即清（仅留"谷斗科技（上海）有限公司"）。
6. commit/push、重建 staging、部署 startupaimanager01.coze.site。

---

## 8. 排期

- **阶段3a（探针）**：✅ 已完成（P1-P4 结论见 4.1、docs/handoffs）。
- **阶段3b（落地）**：撤 JSON、建固定模块树、L0/L1/L2 全部改普通 md（`_guide/_brief/F*`）、投影统一 vectors_only、修 URI 三层契约、数字护栏、改测试；真机连跑两遍成功。
- **阶段4**：责任账本 + 全员署名回填（07）。
- **阶段5**：2C_L1 汇入；看板卡片从 2B 模块 L1 注册；评估用 OV 原生分层装配器替换自研 6000-token 裁剪器。
