# 责任归属层设计（Accountability）

> 历史设计：记忆底座实现已被 [08 自研设计](08-memory-store-selfhosted-design.md) 替代。M4（2026-10-12）代码摘除完成；本文旧运输、地址与启动描述仅供回溯，不是当前操作指南。当前 SQLite + sqlite-vec + 离线 BGE，以及独立 JSON KV，见 [本地记忆](../local-memory.md)。业务分层与来源约束仍保留。

> 状态：**设计稿，待范范确认。** 独立成层、独立成阶段，不属于记忆层。
> 排期：紧接「2B L1/L0」之后作为下一阶段开发（见文末阶段排期）。
> 最高约束：**只记"谁、用什么、在什么输入上、得出什么"的最小凭证；正文一律不进账本。**

日期：2026-10-10；2026-10-11 增补「产物署名（戳）」一节；同日探针后随 06 v2 路线乙修订——2B 三层全普通 md，署名统一走 frontmatter（不再涉及受保护 sidecar）。

---

## 1. 为什么独立成层

系统里每个结论都是某个"脑子"产出的：**唯一的文档管理员工**（按技能承担分类 / 提事实 / 浓缩 / 导览…）或经理 LEADER。
出错时要能回答三句话：**谁做的、当时看到了什么、怎么得出的。**

- 它横切 2A / 2B / 2C，不属于任何一个记忆分区；
- 员工和经理**统一**纳管，经理的拍板与派活是重点；
- 与 2A 同级**外置**，OV 里只放"结论"，账本在外面放"签字和过程指针"。

```
app/
  memory/           # 记什么（2A外置 / 2B / 2C）
  employees/        # 员工怎么运行
  accountability/   # 本层：谁、用什么、怎么得出（横切）
data/
  2a/               # 原件 + 映射
  accountability/   # 行动凭证账本（外置、append-only）
```

---

## 2. 颗粒度（本设计的核心）

### 2.1 什么算"一次行动"，记一条

> 一次模型调用，产出了一个**业务判断 / 事实候选 / 结论 / 指令**，就是一次行动 → 记一条凭证。

记：
- 文档管理员工：语义分类、主体归属、L2 事实提取、L1 聚合、L0 压缩……每次加载相应技能的调用各一条（角色都是 document_manager，以 skill 区分）；
- 经理：每回合的决策 / 结论 / 派活，记一条**主行动**；其下只读工具调用记为**子事件**。

**不记**（非判断性动作，避免噪声）：
- 纯传输 / 健康检查 / ping / 静态文件 / 普通 DB 读写 / 确定性拼装 / 护栏校验本身。

### 2.2 一条凭证记什么（固定字段，典型 0.3–0.6 KB）

| 字段 | 内容 | 颗粒度 |
|---|---|---|
| `action_id` | 全局唯一（ULID） | 必有 |
| `ts_start` / `duration_ms` | 时间、耗时 | 必有 |
| `role_kind` | `manager` \| `employee` | 必有 |
| `role` | 角色：员工恒为 `document_manager`；经理为 `leader`（工序另看 `skill`） | 必有 |
| `model` / `provider` | 模型、供应商（如 `qwen3.8-max`/`token-plan`） | 必有 |
| `skill` / `skill_version` | 技能名 + 版本 | 必有 |
| `run_id` / `session_id` | OpenClaw 指针，**用于回放，不存正文** | 必有 |
| `company_id` | 标的公司 | 必有 |
| `causal` | `parent_action_id` + `trigger`(user/manager/system) + `job_id`：**命令链** | 必有 |
| `inputs` | 列表项仅 `{kind, ref, sha256}`：输入块/L2事实/L1/L0/消息的**外部引用 + 指纹** | 必有 |
| `output` | 仅 `{kind, ref, sha256, item_count}`：产出**指针 + 指纹** | 必有 |
| `status` | `ok`/`error`/`unresolved`/`timeout` | 必有 |
| `error_kind` / `short_reason` | 失败归类 + **截断 ≤200 字**的原因 | 失败时 |
| `attempt` | 第几次尝试（重试每次都记，不覆盖） | 必有 |
| `prompt_ref` | 模板 id/版本 + `prompt_chars` + `prompt_sha`，**不存 prompt 全文** | 必有 |
| `tokens` | prompt / completion token（拿得到才记） | 可选 |

经理主行动的**子事件**（工具调用）只记：`tool 名 + 状态 + 耗时 + 结果 sha256`，**不记返回体**。

### 2.3 坚决不存（防止账本变垃圾堆）

- ❌ 员工 / 经理**输出正文**——正文在记忆层 / data 原处，账本只留 `ref + sha256`；
- ❌ 工具返回的大 payload、文档解析全文、表格全文、图片 / base64；
- ❌ system prompt 全文——只留版本 + hash；
- ❌ 用户对话全文——在 2C_L2；
- ❌ 输入内容的重复快照——只存指纹。

> **怎么回放**：`run_id/session_id` 调出 OpenClaw 会话过程；`inputs[].ref` 取当时输入；
> 各处 `sha256` 校验内容是否被改动。OpenClaw 会话过期时，仍可靠 2A 原件 + 记忆层 + 指纹还原结论依据。

### 2.4 规模直觉

单条凭证目标 **< 1 KB**。量级参考：处理 1 份文件通常产生 3–10 条行动（分类 / 归属 / 若干事实块 / L1 / L0），
约几 KB；系统长期运行账本为"小而密"的索引，不承载内容，故不会膨胀。

---

## 3. 存储模型

- **外置账本**：`data/accountability/yyyy-mm/actions-*.jsonl`，**append-only（只追加、不改不删）**，按月分片；
  与 `data/2a` 平级，**不进 OV**，不随 OV 重建丢失。
- **可重建索引**：另用 SQLite 维护查询索引（按 company / file_hash / run_id / session_id / parent_action_id），
  索引损坏可从 JSONL 全量重建，不作为真相源。
- **命令链**：`causal.parent_action_id` 把"经理下令 → 员工执行 → 结果回流"串成一条，可顺链 / 逆链回查。
- **保留策略**：默认长期保留；只允许对明确标记的测试数据清理并留删证；正常业务凭证不物理删除。

---

## 4. 产物署名：账本是「账」，产物上只盖「戳」

责任归属要有两层，缺一不可：

- **账（权威）**：第 3 节的外置 append-only 凭证账本——能跨文件查询、能记失败/重试/无产物动作、能串命令链、防篡改。
- **戳（可读签名）**：在每一份**产物文件**上盖一个最小署名，让人/模型打开材料就地看到"这是谁、哪次干的"，并能顺着 id 回账本查全过程。

**原则：戳里只放一个 `attribution_id`（+ 少量人类可读字段），绝不复制账本正文。** 一份材料对应一条（聚合类可多条）凭证 id；详情一律回账本取，避免双写不一致和篡改面。

### 4.1 三类产物的署名位置（路线乙：2B 三层全普通 md）

| 产物 | 类型 | 署名方式 |
|---|---|---|
| **bL2 事实卡** `facts/F<id>.md` | OV 普通 Markdown 文件 | **YAML frontmatter**（见 4.2 模板）。源码 `storage/abstract_overview.py` 明确：只有 .abstract/.overview 走 OKF 解析，**普通 Markdown 的 frontmatter 算用户内容，原样保留、绝不隐式解析或丢弃** |
| **bL1 简报 / bL0 导览** `_brief.md` / `_guide.md` | OV 普通 Markdown 文件（路线乙，不占 sidecar） | 同样用 **YAML frontmatter**（普通 md 原样保留），聚合产物盖其依据的**一批** `attribution_id`。OV 受保护的 `.abstract.md/.overview.md` 我们不读不写，故不存在"frontmatter 被丢"问题 |
| **2A 原件 / 映射 md** | 原件不可改 | **绝不污染原件**；署名落在映射 md 的 frontmatter / 外部登记，原件哈希本身就是指纹 |

> 探针后 2B 三层全部是普通 md，署名方式统一为 frontmatter，只放 `attribution_id`（+ 少量人读冗余摘要）。确定性检索需要时可另用 `set-tags`，但不再依赖受保护 sidecar。

### 4.2 普通 md 事实卡的 frontmatter 模板（示例）

```markdown
---
attribution_id: attr_01J0EXAMPLE0001
clerk: document_manager
skill: finance-fact-extraction
skill_version: v3
model: qwen3.8-max
provider: token-plan
source_sha256: 727dd0ccda542e9c14a4a9b1357ac898190636589a90398acceea5b04772fd28
---
# 事实 F12
- 主体：谷斗科技（上海）有限公司
- 属性：净利润
- 期间：2024
- 取值：180 万元
- 出处：谷斗科技-经营与财务摘要.xlsx 第 2 页 · <source_span 坐标>
> attribution: attr_01J0EXAMPLE0001
```

- frontmatter 是 Markdown 生态通用方案（GitHub/Obsidian/Hugo 均认），人读直观、机器易解析，且不影响正文。
- `clerk/skill/model/provider/source_sha256` 是**便于人看的冗余摘要**；权威值以账本中该 `attribution_id` 凭证为准，二者冲突时以账本 + 指纹判定。
- bL1/bL0 同为普通 md，frontmatter 模板一致（聚合层 `attribution_id` 为一批 id）；正文页脚可留一行同样的人读署名，权威字段只查账本。

### 4.3 戳与账的缝合规则

1. 员工产出**先落账本拿到 `action_id`，再把该 id 盖到产物**（戳引用账，不反向）。
2. **无戳即无账、无账不入记忆层**：产物缺 `attribution_id` 或 id 在账本中不存在 → 护栏拒收（与第 5 节"无签字不入库"一致）。
3. 聚合产物（L1/L0）盖其**所依据的一批行动 id**；回查时既能看"谁压的 L1"，也能顺 sources 下钻到每条 L2 的署名。
4. 戳可被人看见、但**不作为可信凭据本身**——是否被篡改，用账本里的输出 `sha256` 比对产物即可发现。

---

## 5. 捕获点与护栏（无业务规则）

- **唯一捕获口**：在员工 / 经理调用 OpenClaw 的统一入口（`employees/worker.py` 及经理运行适配层），
  每次执行后**原样抽取**元数据生成凭证；任何角色都绕不开，且后端不替角色写业务内容。
- **无签字，不入库**：业务产出写入记忆层时 `require(action_id)`——缺凭证一律拒收；产物署名（统一 frontmatter）由投影层在写入时机械加盖，员工只负责给结论、不负责手写署名格式。
- **账本不可用时降级**：不产生"无签字结论"，系统转为只读 / 告警，而非放任裸结论入库。
- 凭证捕获、署名加盖、指纹校验都是**护栏与传输**，不含任何关键词、别名、映射等业务判断。

---

## 6. 阶段排期

| 阶段 | 内容 |
|---|---|
| 阶段3（当前） | 2B L1/L0（见 `06-2b-l1-l0-design.md` v2，路线乙：三层全普通 md `_guide/_brief/F*`）：**先预留 `attribution_id` 位**（各 md frontmatter 带空占位），不建账本、不强签名 |
| **阶段4（本设计）** | 建 `app/accountability/` + `data/accountability/`，改造员工/经理统一入口，全量接入；落地三类产物署名（2B 各 md frontmatter / 2A 映射登记）；给已建 L1/L0/L2 补凭证链与回填署名 |
| 阶段5 | 2C_L1 沉淀进 2B_L1；看板卡片改从 L1 注册（卡片携带 `attribution_id`，可点开回查） |

---

## 7. 验收标准

1. **大小护栏**：单条凭证 ≤1KB；凭证中不得出现正文、base64、超长 reason、prompt 全文（测试自动断言）。
2. **颗粒度吻合**：跑一批模拟任务，账本条数 == 实际"判断性行动"数，不多（无传输噪声）不少（无漏签）。
3. **可回放**：任选一条，能经 run_id/session_id + 输入引用 + 指纹还原过程；命令链可顺 / 逆追溯到具体角色。
4. **经理 + 员工全覆盖**：均有凭证；缺凭证的业务产出无法入库；账本故障时系统降级只读。
5. **署名落位正确**：2B 各普通 md（`F*/_brief/_guide`）frontmatter 保留 `attribution_id`（真机读回验证 OV 未丢弃）；不写 OV 受保护 sidecar；2A 原件零改动。戳↔账 id 双向可查；聚合产物可下钻到每条 L2 署名。
6. append-only、索引可从 JSONL 重建；全量 pytest 全绿（跑前停服）。
7. 真机验收、测完即清（仅留"谷斗科技（上海）有限公司"）；commit/push、重建 staging、部署线上只读窗口。
