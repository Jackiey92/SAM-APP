# 事实树 · 段B1 交接详档（叶子投影内核，默认关闭）

- 日期：2026-10-14；作者：饭团；执行者：CRIS
- 性质：纯后端、确定性护栏与投影，**默认关闭、不接主流程、不改既有行为**
- 权威设计：`docs/architecture/09-facts-tree-design.md`（v1.2）。本档只把“叶子怎么存、后端怎么验”
  落到现有代码，不重复设计论证。冲突一律停手问饭团，禁止自行解释。
- 段A 已落地：`app/facts_tree/{template_packs,company_binding,tree_meta}.py`、`app/module_tree.py`、
  段A 开关常量 `app/settings.py: SAM_FACTS_TREE`、`paths` 里的叶子/树 URI helper。

## ★ 命名与现状对齐裁决（2026-10-14，以段A实际代码为准，覆盖下文任何不一致措辞）

本档起草时用了几个与段A落地代码/设计稿不一致的名字。**一律以本段裁决为准，不要再停下来问同名问题。**

1. **树枝字段统一叫 `trunk`，不叫 `module`。**
   - 叶子 frontmatter 字段是 `trunk: bizov`（设计稿 §4.2、段A `LeafCode.trunk` 一致）。
   - 下文凡写“frontmatter 的 `module`”一律读作 `trunk`；叶子四段/护栏里出现的“leaf.module / 该事实模块”
     一律读作 **leaf 的 trunk**。
   - 仅在内部关系到原子事实表时才出现 `module_key`（facts 表既有列名，保持不改）：
     叶子校验“叶所属 trunk 必须等于其引用 facts 的 module_key”（trunk 码与 module_key 码同一套 10 码，见 `module_tree.MODULES`）。
2. **数据明细段：s 型必备、n 型可省（以设计稿 §4.3 为准）。**
   - 不是“所有叶子四段齐全”。`## 概览 / ## 正文 / ## 编辑历史` 三段所有叶子必备；
     `## 数据明细` 仅 **s 型必备**，**n 型可省**。形态取自模板包 `LeafCode.shape ∈ {n,s}`。
   - n 型省略数据明细时，护栏 2.2.5 中“数据明细数字”一项自然为空，但**正文数字护栏不放松**：
     正文每个数字仍须命中“本叶引用 facts 并集”或“脚注对应净稿 quote 逐字”，二选一，无源即拒。
   - 段顺序仍为 概览→正文→（数据明细，s 必填/n 可省）→编辑历史；n 型缺该段合法，s 型缺该段拒绝。
3. **复用段A真实接口名，禁止另造平行抽象：**
   - 绑定存储：`app/facts_tree/company_binding.py: CompanyBindingStore`（方法 `get/register/bind_pack/mark_locked/is_locked/read_tree_meta`）。
   - 模板包：`app/facts_tree/template_packs.py: load_pack() / validate_pack() / available_packs()`，数据类 `LeafCode(code, trunk, title, shape, scope)`、`TemplatePack`。
   - 开关：常量 `app/settings.py: SAM_FACTS_TREE`，已有闸门 `app/facts_tree/tree_meta.py: require_enabled()`。
     B1 直接复用 `require_enabled()`（或读同一常量），**不要**再新造一个 `facts_tree_enabled()` 函数/第二处环境变量。
4. **URI helper 沿用段A现有签名，只补缺、不改名：**
   - 现有：`leaf_uri(company_id, directory, file_name)`（directory=树枝中文名，如 `02-业务概览`；自动落到 `<树枝>/leaves/<file>.md`）、
     `tree_meta_uri(company_id)`、`trunk_doc_uri(company_id, directory, doc)`（doc ∈ {trunk-brief.md, trunk-guide.md}）。
   - 需新增（命名/校验风格对齐现有，集中在 `app/memory/paths.py`，复用 `_segment/_trunk_directory/identifier/FACTS_ROOT`）：
     - `conflict_uri(company_id, file_name)` → `sam://facts/<company>/待办与冲突/<file_name>.md`（系统枝在公司根，不收树枝 directory）；
     - `tree_brief_uri(company_id)` → `sam://facts/<company>/tree-brief.md`；
     - `tree_guide_uri(company_id)` → `sam://facts/<company>/tree-guide.md`。
   - 叶子文件名：`file_name = f"{leaf_code}-{slug}.md"`，slug 取模板 title 的稳定 slug。**同一 leaf_code 跨版本文件名恒定不变**
     （首次发布即定，禁止随标题改写漂移）；具体 slug 规则可在 leaf_store 内定一个确定性纯函数并单测锁定，不要引第三方 slug 库。

## 0. 硬约束（违反即返工）

1. 只本地 `git commit`，**不 push、不部署**。零新增第三方依赖。
2. 后端**只做结构/引用/出处护栏**，不写任何行业词、关键词、正则、枚举映射、写死公式；
   “事实归到哪片叶子”“正文怎么写”一律是员工/上游决定，后端只校验合法性（叶子必须属于该公司绑定模板）。
3. 段A 开关 `SAM_FACTS_TREE` 保持默认关闭；B1 新代码全部在 `settings.facts_tree_enabled()` 闸门后，
   关闭时全系统行为与现状**逐字节一致**（既有 1113 测试必须仍全绿）。
4. 不改原子事实 SQLite 正本结构语义；不删既有 L1/L0 代码路径（B1 只**新增**叶子内核，
   “停用原子卡 md 投影 / 切换主流程”在段B3 开闸门时做，B1 不动）。
5. 测试只用临时库/临时目录，不写真实公司库；不碰 `harness-openclaw/`。
6. 每完成一块自测，到约定边界停手写 `docs/handoffs/facts-tree-segment-b1-report.md`，等饭团黑盒，不自行连做 B2/B3。

### 冲突件（待办与冲突）裁决（2026-10-14 第二次澄清）

冲突件是**独立形态**，不是业务叶子，也不要求 `leaf_code/trunk` 命中模板叶子：

- frontmatter：`leaf_code = conflict-<trunk>-<metric>-<seq>`（如 `conflict-hr-rdstaffratio-0001`），
  `trunk: 待办与冲突`（系统枝，不是业务树枝），必填关联字段 `target_trunk`（如 hr）、
  `target_leaf`（如 L-hr-04）、`metric`（指标短码）。
- 文件名 `conflict-<trunk>-<metric>-<jobid>.md`，**禁用下划线**，metric 一律短码连字符
  （`rd_staff_ratio` → `rdstaffratio`），与现有 `_segment()` 命名规则一致。
- `publish_conflict(...)` **只接受 `status: conflict`**，不接受 archived。裁决后转 archived 是
  后续棒次的**独立归档动作**（胜出值并入 target_leaf 后归档隐藏），B1 不实现、不由本接口写。
- 冲突件正文引用/数字护栏同正常叶子（脚注坐标真实、数字命中 facts 或 quote），但**不校验**其 leaf_code 命中模板。
- 冲突样例已按本裁决修正（补 target_trunk/target_leaf/metric、去下划线），以仓库内样例文件为准。

## 1. 数据模型（向后兼容迁移，照搬 initialize.py 现有 PRAGMA 风格）

### 1.1 事实↔叶子归属：用独立映射表，**禁止 ALTER facts 表**（第三次澄清，覆盖原方案）

原方案给 `facts` 加 nullable `leaf_code` 列，**作废**。原因：系统多处用 `SELECT * FROM facts` 转字典
（`l2_consolidation.py` 三处、`provider.py` `_fact_rows`、`memory_map.py` 一处，不止两处），加列后即使
开关关闭、值为 NULL，原子卡与模块 fact_sources 的序列化也会多出 `"leaf_code": null`，无法保证关闭态逐字节不变；
在每个读取点补过滤既易漏点又把新字段渗进旧路径。

改为**独立映射表**，`facts` 正本表零 ALTER、零新列、所有既有 `SELECT *` 路径字节不变：

```sql
CREATE TABLE IF NOT EXISTS fact_leaf_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL,
    fact_id      INTEGER NOT NULL,
    trunk        TEXT NOT NULL,        -- 必须等于该 fact 的 module_key（服务层校验，非 DB 触发器）
    leaf_code    TEXT NOT NULL,        -- 该公司绑定模板中存在的叶子码
    source_file  TEXT NOT NULL,        -- 归属时所在原件 sha256，用于文件级重归属
    job_id       TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(company_id, fact_id, leaf_code)
);
CREATE INDEX IF NOT EXISTS idx_fll_leaf   ON fact_leaf_links(company_id, leaf_code);
CREATE INDEX IF NOT EXISTS idx_fll_file   ON fact_leaf_links(company_id, source_file);
```

- leaf_code/trunk 合法性（叶子属绑定模板、trunk==该 fact 的 module_key）在叶子发布服务里校验，不做 DB 触发器。
- 该表**只被 facts_tree 内核读写，且在 `settings.facts_tree_enabled()` 闸门后**；关闭态任何旧代码路径都不碰它。
- 一条事实可挂多叶（UNIQUE 是 fact+leaf，不限制一对一）；B1 只建表+在叶子发布时随叶子事务写归属，
  B2 才用于增量去重/重归属。叶子护栏“数据明细 fact_id 不得跨 trunk/越权”改为：解析事实行取 module_key（不依赖 facts 新列）+ join 本表校验。
- 你工作区里已写的“给 facts 加 leaf_code 列 + initialize.py ALTER + 相关 SELECT/快照”请回退为零（facts DDL 与 initialize.py 恢复原样），改用本映射表；保留 leaf_revisions 等其余设计。

### 1.2 新表 `leaf_revisions`（叶子编辑历史，append-only）
在 `app/db/schema.py` 增加（initialize_memory 风格的 IF NOT EXISTS + 索引）：
```sql
CREATE TABLE IF NOT EXISTS leaf_revisions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id   TEXT NOT NULL,
  leaf_code    TEXT NOT NULL,
  version      INTEGER NOT NULL,            -- 同一 leaf 内单调 +1
  content_sha  TEXT NOT NULL,               -- 本次叶子 md 的 sha256
  source_files TEXT NOT NULL,               -- JSON 数组：本次引用到的原件 sha256（去重排序）
  fact_ids     TEXT NOT NULL,               -- JSON 数组：本次数据明细引用的 facts.id（去重排序）
  owner_skill  TEXT NOT NULL,               -- 固定 'fact-leaf'
  attribution_id TEXT,
  change_note  TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE(company_id, leaf_code, version)
);
CREATE INDEX IF NOT EXISTS idx_leaf_rev_company ON leaf_revisions(company_id, leaf_code, version);
```
append-only：服务层只 INSERT，不提供 UPDATE/DELETE；回滚=发一个新版本，不改旧行。

## 2. 叶子发布服务（核心，新增文件）

新增 `app/facts_tree/leaf_store.py`，提供 `LeafPublishService`。参考既有
`app/memory/company_facts/l1_brief.py` 的 `projection_lock / read_optional / publish_pair / 两阶段` 模式实现，
复用 `publish_pair`（它已处理 provider 多文档事务与回滚）。

### 2.1 输入
`publish_leaf(*, company_id, leaf_code, markdown, source_files, fact_ids, attribution_id=None,
change_note=None, thread_id=None)`：
- `markdown`：员工产出的**完整叶子 md**（四段式，见设计稿 §4）。后端不生成正文。
- `source_files: list[str]`、`fact_ids: list[int]`：员工声明的出处。

### 2.2 后端护栏（全部确定性校验，任一不过 => 抛 `LeafRejected`（新异常，语义同 WorkerUnavailable 的“产物不可用”，不是系统故障）；不猜、不改写、不自动补）
1. 闸门：`app/facts_tree/tree_meta.require_enabled()`（读同一常量 `settings.SAM_FACTS_TREE`）为假直接拒绝（防御性，正常 B1 不会被主流程调到）。
2. 叶子合法性：从段A `CompanyBindingStore.get(company_id)` 读公司绑定包（缺绑定则拒绝），
   用 `load_pack(pack)` / `validate_pack(pack)` 校验 `leaf_code` 在该包 `leaves` 中存在，
   且该 `LeafCode.trunk == 引用 facts 的 module_key`（leaf_code 前缀即 trunk 码，需与模板定义一致；跨 trunk 引用拒绝）。
3. **段落结构**（用 markdown 标题解析，不做语义判断）：按序必须有
   `## 概览`、`## 正文`、`## 编辑历史`；`## 数据明细` **s 型必备、n 型可省**（形态以模板 `LeafCode.shape` 为准）：
   - s 型缺 `## 数据明细` => 拒绝；n 型缺该段 => 合法。
   - 不允许出现“缺口/待补/缺失”这类由系统代标缺口的独立标题段。注意后端**不做正文关键词审查**
     （避免业务正则），只校验二级标题名不得是这组缺口标题；正文句子里出现这些词不管。
4. **数据明细表 fact_id 引用完整性**（该段存在时）：`## 数据明细` 表格中出现的每个 fact_id 必须：
   - 存在于 `facts`、`company_id` 匹配、`status='verified'`、`superseded_by IS NULL`、`valid_to IS NULL`；
   - 该 fact 的 `module_key == leaf.trunk`；
   - 包含在传入 `fact_ids` 集合内（声明与实际一致）。
5. **正文/数据明细数字护栏**：复用 `company_facts/numeric_guard.py`。
   - `## 数据明细` 的数字：用 `validate(text, {fact_id: fact}, kind='f')`，规则与 L1 brief 一致（每个数字必须能在其引用事实里找到）。
   - `## 正文` 的数字：正文允许出现叙事数字，但每个阿拉伯数字都必须能在“本叶引用的 facts 并集”里找到，
     或在“脚注对应的净稿原文 quote”里逐字出现（二选一）。拿 quote 校验需要净稿文本，见 2.5。
     实现上允许注入一个 `quote_resolver(footnote)->str|None`，B1 单测用假 resolver；真实接线在 B2。
     resolver 为 None 时，正文数字护栏退化为“仅 facts 并集”，并在测试里显式覆盖两种路径。
6. **脚注引用形式与坐标**：正文每个自然段末必须有至少一个 `[^k]`；文末必须有对应脚注定义
   `[^k]: <原件名或sha> <坐标>`。坐标形如既有 `docx:body/77/paragraph`。后端校验：
   - 所有 `[^k]` 引用都有定义、无悬空、无重复定义；
   - 每条脚注的 sha/原件名能解析到该公司 `source_files` 中真实存在的一份原件（只验 sha 在库，不验坐标文本语义）。
7. **编辑历史 append-only**：`## 编辑历史` 必须是追加表；新版本要保留旧版本的全部历史行，
   再加一行（版本号 = 旧最大版本 +1，首个版本=1）。后端比对库里上一版 md 的编辑历史行集合：
   新集合必须是旧集合的超集（允许格式空白归一化后比较），否则拒绝（防止改写历史）。
8. **frontmatter**：YAML（pyyaml 已在仓库）。必填 `leaf_code/trunk/title/company_id/status/
   latest_period/sources/source_file_count/version/created_at/updated_at/owner_skill`；
   - `leaf_code/trunk/company_id` 必须与入参/绑定一致（字段名是 `trunk`，值如 `bizov`）；
   - `sources` 排序去重后必须等于 `source_files`；`source_file_count == len(sources)`；
   - `version` 必须等于将写入的版本号；`owner_skill == 'fact-leaf'`；
   - `status ∈ {active, archived, conflict}`（普通叶 active；冲突叶见 2.4）。
   YAML 解析失败/缺字段/不一致 => 拒绝。

### 2.3 两阶段发布（照抄 _Stage/publish_pair 思路）
- 在 stage 内存里组装：叶子 md 写到段A 现有
  `paths.leaf_uri(company_id, directory, file_name)`（directory 为该 trunk 的中文名目录，取自 `module_tree`；
  file_name = `f"{leaf_code}-{slug}.md"`，slug 由模板 title 确定性生成）。**同一 leaf_code 跨版本 URI/文件名稳定不变**，
  slug 首次发布即定（记入 leaf_revisions 或同库 leaf 索引），后续版本沿用，禁止改名漂移。
- 同一事务：INSERT leaf_revisions（version 取 `SELECT COALESCE(MAX(version),0)+1`，带 `projection_lock`）；
  同时对本叶 `fact_ids` 逐条 UPSERT `fact_leaf_links(company_id, fact_id, trunk, leaf_code, source_file, job_id, created_at)`
  （trunk 取事实行 module_key，source_file 取该事实 source_file）。任一失败整体回滚，不留半成品。
- publish_pair 成功后再提交 revision；revision 与映射写入在同一连接事务。
- 幂等：内容 sha 与当前活动版本相同则 no-op，不新增 revision、不重写文件、不重复插映射（参考 module_projection 的 byte-identical 跳过）。

### 2.4 冲突叶（独立目录，不与确定叶混写）
- `publish_conflict(...)`：把冲突件发布到公司根的系统枝 `待办与冲突/`（不挂在某个树枝目录下），
  文件名沿用设计稿 `conflict-<trunk>-<metric>-<jobid>.md`，URI 用本档裁决新增的
  `paths.conflict_uri(company_id, file_name)`（拼为 `sam://facts/<company>/待办与冲突/<file_name>`）；
  frontmatter `status: conflict`。冲突叶**不进** trunk/tree 的 active 汇总。
- 冲突叶的引用/数字护栏同 2.2，但不要求四段式完整（允许“双候选+处置”结构，见仓库内样例
  `docs/architecture/assets/facts-tree/样例_待办与冲突-研发人员占比.md`），
  后端只验 frontmatter/status/出处 sha 真实/不伪造数字，不强制编辑历史超集（冲突件可迭代，保留自身历史即可）。

### 2.5 净稿 quote 解析口（只定义协议，不接真实存储）
- 定义 `QuoteResolver` 协议：`resolve(*, file_hash, loc) -> str | None`（返回该坐标净稿原文）。
- B1 提供一个 `NullQuoteResolver`（恒返回 None）和一个 `InMemoryQuoteResolver`（测试用，坐标->文本字典）。
- 真实“从文档库 parsed/<sha>.md 按坐标取块”的 resolver 在 B2 接（由文件管理员技能 B 的受控取件支持），B1 不连。

## 3. trunk / tree 汇总（确定性机械搬运，无员工、无新数字）

新增 `app/facts_tree/rollup.py`：
- `rollup_trunk(*, company_id, trunk)`：读该树枝（trunk，内部对应事实表 module_key）下所有 **active 且有内容**的叶子（不含冲突/空模板），
  机械生成/覆盖 `paths.trunk_brief_uri / trunk_guide_uri` 的 md（trunk-brief=各 active 叶概览+正文要点的
  有序拼接；trunk-guide=叶子清单+每叶一句话定位+latest_period 索引）。
  - 纯搬运：不得出现任何在叶子里找不到原句的数字或结论（用 numeric_guard 对汇总数字做“必须出现在某 active 叶”校验）。
  - 只含 active 叶；空模块不生成 trunk 文件（有内容才显）。
- `rollup_tree(*, company_id)`：同理从各 trunk-brief 机械汇总 `paths.tree_brief_uri / tree_guide_uri`，
  数字必须能追溯到某 active 叶；只列非空树枝。
- 这两个函数 B1 只实现+单测，**不挂任何主流程触发**（B2 在叶子发布后按树枝增量调用，B3 端到端）。
- 复用 `projection_lock`，幂等（内容不变不重写）。

## 4. 与既有投影的关系（B1 只隔离，不切换）

- 既有 `L2ProjectionService`（原子卡 md 投影）、`ModuleProjectionService`（模块 brief/guide）、
  provider `rebuild_projection/_fact_document/_fact_rows` **一律不改行为**。
- 关键兼容保证：**`facts` 表不 ALTER、不加列**（归属走独立 fact_leaf_links），因此这些 `SELECT *` 路径
  连字段顺序都不变，关闭态逐字节一致由快照测试强制。
- B1 叶子发布到的 URI 在 `sam://facts/<company>/...`，与既有 `sam://2b/...` 前缀不同，
  且开关关闭时根本不会被调用；向量索引是否纳入叶子，**B1 不做**（B3 开闸门时统一处理，
  避免现在改 rebuild_projection 影响检索回归）。
- provider `publish_documents` 的白名单若需放行新叶子/汇总 URI：在开关关闭分支保持原白名单，
  仅在 `facts_tree_enabled()` 为真时追加 facts 前缀根文件名，且必须有快照测试锁定关闭时白名单不变。

## 5. 测试（pytest，临时库；新增测试文件 tests/test_facts_tree_leaves.py、tests/test_facts_tree_rollup.py）

至少覆盖：
1. facts 表零变更：断言 facts DDL 无 leaf_code 列、initialize.py 不对 facts 做 ALTER；既有 `SELECT * FROM facts`
   序列化快照在开关关闭态逐字节不变（用迁移前/后原子卡与模块 fact_sources 快照锁定）。
   fact_leaf_links 表：新库建成；老库 initialize 后补建；事实↔叶子归属随发布写入、跨 trunk 写入被服务层拒绝。
2. leaf_revisions：版本自增、append-only、唯一约束、幂等 no-op 不增版本。
3. 叶子护栏逐条：未知叶子/trunk 不符/绑定缺失/frontmatter 不一致/必备段缺失（s 型缺数据明细拒绝、n 型缺数据明细放行）/脚注悬空/数据明细 fact_id
   越权或跨 trunk/数字不在 fact 或 quote/编辑历史被改写 => 全部拒绝且不产生半成品文件、不写 revision。
4. 正文数字护栏两路径：有 QuoteResolver（quote 逐字命中放行、不命中拒绝）；NullResolver（只认 facts 并集）。
5. 脚注 sha 必须是库内真实原件；伪造 sha 拒绝。
6. 冲突叶：独立 URI、status=conflict、不进 trunk/tree active 汇总。
7. rollup：只汇总 active 有内容叶；空模块/空公司不产文件；汇总里出现无源数字被数字护栏拒绝；幂等。
8. URI 稳定：同叶两版 slug 不变；开关关闭时发布被拒、既有白名单/投影快照不变。
9. 全量：`python -m pytest -q` 结束必须为 **既有用例数（段A后 1113 passed / 1 skipped）全绿 + 新增用例全绿**；
   自己在报告里给出新增用例数与最终 passed 总数、耗时。

## 6. 提交与停手

- 完成并自测后，commit message：`feat(facts-tree): leaf publish kernel, append-only revisions, trunk/tree rollup guards (segment B1, gated off)`。
  若遇 `.git/index.lock Read-only file system`（已知沙箱问题），不要绕过、不要反复试，照常 `git add` 并在报告里列明已暂存文件，饭团在正常 shell 代提交。
- 只 add 本棒相关文件；`harness-openclaw/`、`__pycache__` 不得进暂存。
- 写 `docs/handoffs/facts-tree-segment-b1-report.md`：新增/修改文件清单、每个护栏对应的测试名、
  最终 pytest 数字、明确“未接线主流程/默认关闭/未动原子卡与模块投影”的自证（grep 证据）。
- 然后停手，等饭团真机黑盒，不进入 B2。
