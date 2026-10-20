# 事实树 B2a 交接详档：真实取件器 + fact-leaf 技能契约 + 叶子构建服务

给 CRIS。本棒在 B1（`0cd8e56`，已黑盒通过）之上做 **B2a**。权威设计是 `docs/architecture/09-facts-tree-design.md` v1.2；B1 详档 `11-facts-tree-handoff-B1.md`；本档是 B2a 唯一施工口径。

## 0. 铁律（与前段相同，先读）

- 只本地 commit，不 push、不部署；只改本仓，不碰 `harness-openclaw/`（既有脏文件）。
- 零新增第三方依赖；后端零业务规则（不得出现行业词、正则归类、关键词、指标枚举、写死公式）。
- 业务判断只能来自员工；后端只做形状/出处/坐标/数字护栏。
- 只跑测试库（`tmp_path`）；不删原件、不删 facts 正本。
- 本棒 **默认开关关闭（`SAM_FACTS_TREE` 未置 1）**，不接入 document_read 主流水线、不改任何现有调用行为；关闭态老产物逐字节不变。
- 遇设计/代码口径冲突：**停手在最后消息列出冲突点**，不自行解释。
- 每段做完跑全量回归，再停手等黑盒，不进入 B2b/B3。

## 0.1 以现有代码为准的命名事实（不要再猜，已核对）

- 模块包：`app/facts_tree/`；开关 `app/settings.py:SAM_FACTS_TREE`；闸门 `app/facts_tree/tree_meta.py:require_enabled()`。
- 叶子落盘：`app/facts_tree/leaf_store.py`
  - 协议 `QuoteResolver.resolve(*, file_hash: str, loc: str) -> str | None`；已有 `NullQuoteResolver`、`InMemoryQuoteResolver`。
  - 服务 `LeafPublishService(db_path, memory, *, quote_resolver=None, company_id=None)`。
  - `publish_leaf(*, company_id, leaf_code, markdown, source_files, fact_ids, attribution, note, job_id=None, previous=...)`（以实际签名为准）。
  - `publish_conflict(...)`；内部 `_sources/_facts/_footnotes/_validate/_publish`。
- 模板：`app/facts_tree/template_packs.py`（注意复数），`load_pack(name)`，`pack.leaves`（每项 `.code/.trunk/.shape/.title`，以实际字段为准）。
- 绑定：`app/facts_tree/company_binding.py:CompanyBindingStore`（`.get(company_id)` 返回含 `template_pack/pack_version` 的行）。
- 汇总：`app/facts_tree/rollup.py: rollup_trunk(*, leaf_store, company_id, trunk)`、`rollup_tree(*, leaf_store, company_id)`、`validate_rollup(...)`。
- 受控取件原型：`app/document_read/dossier.py:Dossier`，方法 `read(source_loc)`（按 elements 的 source_loc 取块）、`range(start,end)`，属性 `.file_hash/.elements/.lines`，构造入参 `(document, *, file_hash, objects_root)`。
- 净稿坐标真实形态（鸿通晟实测）：每段尾部 ` ^[loc docx:body/77/paragraph]`；`document_map.json['elements']` 每项 `{kind, source_loc, start_line, end_line}`；表格 source_loc 形如 `docx:body/16/table`。
- 员工运行时：`app/employees/worker.py:EmployeeRunner.run(*, skill, text, company_id, thread_id=None, ...)`；技能是 `skills/<name>/SKILL.md` 纯文本，由 `config.skill_root` 定位（现有 `skills/fact-brief/`、`skills/fact-guide/`）。
- 既有出处校验可参考但不直接复用其业务字段：`app/memory/company_facts/l2_fact_verifier.py`（quote 去空白后须在块内容命中）。

## 0.2 二次裁决（饭团已核对 B1 源码 leaf_store.py，覆盖正文，以此为准）

CRIS 开工核对发现 3 处 B2a 口径与 B1 实际实现不符，裁决：

1. **严格坐标校验放在 LeafBuildService，不改 B1。** B1 `_validate/_conflict_table` 对 `quote_resolver.resolve(...)` 返回 `None` 是有意宽松（过滤掉 None，允许"数字只来自所引 fact、无原文块"的情形）。B2a 需要更严：在 LeafBuildService 调 B1 之前做**预检**——解析员工 markdown 的全部脚注定义 `[^n]: 原件名 → docx:body/xx/...`，把"原件名"无歧义映射到员工声明的 source_files 之一（sha），对每个 `(sha, loc)` 调 `DossierQuoteResolver.resolve`，**任一返回 None（坐标不存在/未授权/越界）即拒收**。这条不允许通过修改 B1 实现，避免动到 B1 已通过的行为与测试。纯描述句（无数字，如"纳米级涂膜"句）也必须带脚注且坐标真实命中——这是"描述不漏且可溯源"的落点。
2. **frontmatter 与编辑历史由宿主（LeafBuildService）装配，员工不写。** 员工 markdown 只含正文段（`## 概览`、`## 正文`，s 型再含 `## 数据明细`），**不含** frontmatter、**不含** `## 编辑历史`。LeafBuildService 在调用 B1 `publish_leaf` 前组装完整文档：
   - `title`：取绑定模板包中该 leaf 的标题（不允许员工自拟，防止标题漂移）。
   - `leaf_code/trunk/company_id/owner_skill(fact-leaf)`：由叶子定义与公司上下文填。
   - `status`：新叶 `active`（本棒不产生 archived；冲突走 build_conflict）。
   - `version`：查 `leaf_revisions` 该 leaf 最新版本；内容 sha 未变走 no-op，否则 +1（与 B1 `_publish` 的版本判定一致，避免冲突）。
   - `sources/source_file_count`：从员工声明的 source_files 去重推导。
   - `latest_period`：取本叶所引 verified facts 的最大期间（period 为可排序年份/期间字符串；无法判定时用 `unspecified`）。**只做机械取最大，不做任何行业推断。**
   - `created_at`：首版用当前 UTC；已存在叶子沿用首版 created_at（查 leaf_revisions 最早版本）。`updated_at`：当前 UTC ISO。
   - `## 编辑历史`：append 恰好一行新版本，列须符合 B1 `history_rows` 解析格式与"版本集合 1..version、相对 previous 仅新增一行"的校验（先以 previous 正文为基，追加一行；版本/时间/责任技能 fact-leaf/来源指纹或 job_id/change_note=员工 note）。
   - 装配规则属于**格式机械装配**，不得包含行业判断/关键词/补写事实内容。
3. **B1 返回值不改。** `publish_leaf` 仍返回 `{uri, version, changed}`（不新增 updated_at）。LeafBuildService 在发布成功后，用同一连接查 `leaf_revisions` 该 `(company, leaf_code, version)` 的 `created_at` 作为 `updated_at`，连同 uri/version/changed 一并返回。
4. **fact_leaf_links 由 B1 已负责，不要双写。** B1 `_publish`（leaf_store.py 约 438–446 行）已在发布事务内 upsert `fact_leaf_links`（含 trunk/source_file/job_id），冲突件不写 link。LeafBuildService **不得**再单独写该表；只负责前置校验与装配，落盘与 link 全部交给 B1，复用其同事务与回滚补偿。正文 §2.3 第 4 步作废，以此条为准。

## 0.3 三次裁决：fact-leaf 必须正规登记进技能契约（饭团已核实生成链）

CRIS 报告新增 `skills/fact-leaf/SKILL.md` 会使 `tests/test_phase1_catalog.py` 失败（该测试断言"扫描 skills/ 的结果 == `harness-openclaw/skill-registry/catalog.json` 快照"，且把技能名集合写死为原 8 项）。核实结论：`catalog.json` 是 `scripts/build_phase1_catalog.py` **确定性生成并纳入 git 的可评审契约产物**（现有 fact-brief/fact-guide 即按此流程登记），不属于"禁止触碰的 harness 运行时脏文件"。裁决按正规登记处理：

1. **给 `skills/fact-leaf/SKILL.md` 加合法 frontmatter**，须通过 `SkillRegistry.register_markdown`，且 `name` 必须等于目录名 `fact-leaf`。参照 fact-brief：
   ```yaml
   ---
   name: fact-leaf
   description: Build one cited company fact-tree leaf from supplied parsed-document fragments, without inventing facts.
   version: 0.1.0
   metadata:
     sam:
       roles: []
       visibility: workspace
   ---
   ```
   `roles: []` 与 fact-brief/fact-guide 同例（本棒不挂角色、不接调度，B2b/B3 再由文件管理员按名调用）；不得自造 ROLES 之外的角色。
2. **用官方脚本重生成契约产物**：运行 `python scripts/build_phase1_catalog.py`。允许更新并提交它生成的这两个被 git 跟踪的契约文件：`harness-openclaw/skill-registry/catalog.json`、`harness-openclaw/skill-registry/rejected.json`（后者须保持 `[]`，即 fact-leaf 被接受、无拒绝项）。
3. **工具清单必须零变化**：脚本同时重写 `harness-openclaw/tool-registry/manifest.json` 与 `rejected.json`；提交前必须 `git diff --stat` 核实这两个 tool-registry 文件**无变化**（本棒不加插件工具），若有变化说明有误，停手报告。
4. **更新测试写死集合**：把 `tests/test_phase1_catalog.py` 中那段 8 项技能名集合加入 `"fact-leaf"`（变 9 项）；保持 `catalog == artifact`、`rejected == []` 两条断言语义不变。这是正规契约更新，不是放宽断言。
5. **phase3 角色矩阵不受影响**：fact-leaf `roles: []`，不进入任何 `ROLE_SKILLS`，因此 `build_phase3_role_matrix` 与其生成产物/`test_phase3_contracts.py` 应保持不变；请实际复跑确认零 diff、相关测试通过。
6. 本棒**仅允许**提交上述契约文件与 B2a 自身代码/测试；`harness-openclaw/` 下其它**既有的、与本棒无关的未提交改动**仍一律不 stage、不提交（"不碰 harness"针对的是那些运行时脏文件，而非这里点名的生成契约）。

## 1. 目标与非目标

**目标（三件）**
1. 一个**真实取件器** `DossierQuoteResolver`：把 B1 的 `QuoteResolver` 协议接到解析净稿（Dossier 同源数据），按 `file_hash + source_loc` 返回该坐标块的净稿文本；只碰解析净稿与坐标，不碰原件二进制、不越权取全库。
2. **fact-leaf 员工技能契约**：新增 `skills/fact-leaf/SKILL.md`，定义员工"读净稿片段→产出一片叶子"的输入/输出 JSON 契约与写作规矩（四段式、脚注、只陈述不造数）。本棒只交付 SKILL.md 文本与一个**确定性契约适配/校验函数**，不接真实模型（真实模型跑在 B2b/B3）。
3. **叶子构建服务** `LeafBuildService`（确定性编排，后端）：输入"公司 + 目标 leaf_code + 该叶相关净稿片段 + 员工产出对象 + 关联 fact_ids"，做形状/出处/坐标校验后调用 B1 `LeafPublishService.publish_leaf` 落盘，并在同事务写 `fact_leaf_links`。员工产出不达标返回结构化拒收（不自动改、不猜补）。

**非目标（明确不做）**
- 不接 document_read 主流水线、不做"文件进来自动判枝派叶"的调度（那是 B2b）。
- 不调用真实大模型、不改 harness/openclaw、不新增 sam-memory 插件工具（B2b/B3）。
- 不改 facts 正本表、不动原子卡/模块 brief/guide 现有投影。
- 不做建档选包 UI、不做开关默认打开（B3）。

## 2. 施工要求

### 2.1 真实取件器 `DossierQuoteResolver`
- 位置建议：`app/facts_tree/quote_sources.py`（新文件）。
- 构造：`DossierQuoteResolver(documents)`，其中 `documents` 是"任务允许访问的净稿集合"，最小可由调用方传入一个 `{file_hash: document}` 映射（`document` 即 Dossier 构造用的同构 dict：含 `markdown` 与 `document_map.elements`）。**不允许**自行打开任意 DB 路径或按 company 全库读取——取件范围由调用方（任务）显式授予。
- `resolve(*, file_hash, loc) -> str | None`：
  - 仅在 `file_hash` 属于已授予集合时返回；否则 `None`（不得抛权限外内容）。
  - 复用 `Dossier.read(loc)` 的取块逻辑（可直接内部构造一个 Dossier，或抽出共享纯函数；不得复制出第二套坐标算法导致漂移）。
  - 坐标不存在/越界 → 返回 `None`（交给上层判出处失败），不伪造空串。
- 不提供"取整篇/取全库/取原件字节"能力；对象/图片不在本棒。
- 纯结构与坐标逻辑，零行业规则。

### 2.2 `skills/fact-leaf/SKILL.md` 技能契约
- 新建目录与文件（参考 `skills/fact-brief/SKILL.md` 的写法风格，但产出是**一片叶子**，不是模块简报）。
- 员工**输入**（由编排拼进 text，B2b 实现；本棒只在 SKILL.md 里写清契约）：
  - 目标公司 id、目标叶子 `leaf_code` 与叶子标题/形态（shape: n|s）、所属 trunk；
  - 该叶相关的**解析净稿片段集合**：每段带 `file_hash`、`original_name`、`source_loc`、片段文本；
  - 该叶可用的已核实原子事实 id 列表（id+指标+期间+值+单位+来源坐标），仅可引用、不可改写数值。
- 员工**输出**：单个 JSON 对象（不是数组），字段：
  - `leaf_code`（必须等于输入目标）；
  - `markdown`：**只含正文段**（`## 概览 / ## 正文`，s 型再含 `## 数据明细`），**不要写 frontmatter、不要写 `## 编辑历史`**（这两块由后端宿主机械装配，见 §0.2-2）；叙事数字就地写；
  - `source_files`：本叶用到的原件 sha 列表（只能来自输入片段）；
  - `fact_ids`：本叶引用的已核实事实 id（只能来自输入列表）；
  - `note`：本次变更摘要（一句话）。
- 写作规矩（写进 SKILL.md，硬性）：
  - 只陈述原件有的，不评价、不脑补、不预测；未来值须显式标注"目标/计划/预计"。
  - 每个自然段、每张表末尾用标准 markdown 脚注 `[^n]`；文末脚注定义形如 `[^n]: 原件名 → docx:body/77/paragraph`（原件名用输入给的 original_name；坐标逐字用输入 source_loc，禁止自造）。
  - 数字必须能在引用片段原文或所引 fact 中逐字（去空白）命中；数字冲突不取舍，两个都写并在本棒交给冲突流程（B2a 仅要求员工不合并冲突；冲突件落盘由后端冲突入口，B2b 串）。
  - 不标"缺口/待补/无信息"；没有就不写。
  - 叶子标题用公司视角通用名，不出现"前五大/投资人/尽调"视角词。
- 形态规则：s 型必须含 `## 数据明细` 表格；n 型可省该段（与 B1 `_validate` 口径一致）。
- 同时在 `app/facts_tree/` 增加一个**确定性契约模块**（建议 `leaf_contract.py`）：
  - `parse_employee_leaf(raw: dict) -> dict`：只做**形状**校验与字段归一（必填键、类型、去 JSON fence 已在 runner 层；leaf_code 是 str、markdown 非空 str、source_files 是 str 列表、fact_ids 是 int 列表、note str）。不做语义、不补内容、不做行业判断。不合规抛 `LeafRejected`。
  - 不允许在此模块写任何关键词/指标枚举。

### 2.3 `LeafBuildService`（确定性编排）
- 位置建议：`app/facts_tree/leaf_build.py`。
- 构造注入：`LeafPublishService`（或其 db/memory/quote_resolver 同参）、`DossierQuoteResolver`、`CompanyBindingStore` 所需连接；不得自建全局连接。
- 方法建议：`build_leaf(*, company_id, leaf_code, employee_payload, job_id=None)`。员工 payload 只有正文（概览/正文/[数据明细]）+ source_files + fact_ids + note，**无 frontmatter、无编辑历史**。
- 流程（与 §0.2 裁决一致，冲突以 §0.2 为准）：
  1. `require_enabled()`（开关门）。
  2. `parse_employee_leaf` 形状校验；`leaf_code` 必须在该公司绑定模板包内（复用 B1 的 `_leaf`/load_pack 逻辑，不要复制模板表）。
  3. **坐标预检**：把员工每条脚注定义映射到声明 source_files 的 sha，逐一对 `DossierQuoteResolver.resolve(file_hash, loc)`，任一为 None 即 `LeafRejected`（纯描述句同样要求真实坐标，见 §0.2-1）。
  4. **宿主装配**：按 §0.2-2 组装 frontmatter（title 取模板、version/created_at/updated_at、sources/count、latest_period 机械取所引事实最大期间、status=active）与追加一行 `## 编辑历史`，把员工正文拼成 B1 要求的完整 markdown。
  5. 调 B1 `publish_leaf` 完成全部既有护栏（来源 sha 在本公司、脚注坐标、数字 union、trunk 一致、四段式、版本/编辑历史）。B1 已实现的校验与 `fact_leaf_links` 同事务写入、回滚补偿**一律复用，不得绕过、重写或双写**。
  6. 成功后查 `leaf_revisions.created_at` 作为 updated_at，返回 `{uri, version, changed, updated_at}`（B1 返回值不改，见 §0.2-3）。
- **冲突**：本棒提供一个薄封装 `build_conflict(...)` 透传到 B1 `publish_conflict`（同样过真实取件器与脚注/数字护栏）；不要求自动识别冲突（识别是员工/B2b 的事）。
- 不改 B1 任何已通过测试的函数行为；如确需在 B1 暴露可复用点（例如把 `_leaf` 提为内部可调用），用最小重构且保持现有测试全绿。

## 3. 测试要求（pytest，tmp_path，假员工/假净稿，不连真模型）

新增一个测试文件，覆盖：
1. `DossierQuoteResolver`：
   - 用鸿通晟同构的最小净稿夹具（至少含一个 paragraph 块和一个 table 块坐标）能按 `(sha, loc)` 取回正确文本；含"纳米级涂膜技术，提升产品精度"的块能被逐字取回（**描述不漏的取件层证据**）。
   - 未授予的 file_hash → None；不存在 loc → None；不暴露整篇/原件。
2. `parse_employee_leaf`：合法对象通过；缺键/错类型/空 markdown/leaf_code 非 str/fact_ids 含非 int 等分别拒收。
3. `LeafBuildService`：
   - 开关关闭 → 拒绝（`LeafRejected`/RuntimeError，与 B1 闸门一致）。
   - 用假员工 payload（**仅正文，无 frontmatter、无编辑历史**）+ 临时库（先在库内造一条绑定 generic 的公司、一条该 trunk 的 verified fact、一条 source_file 记录）成功落一片 s 型叶：宿主正确补齐 frontmatter（title 取模板、status active、sources/count、version=1、created_at=updated_at、latest_period 取所引事实期间）与一行编辑历史；URI 在 `sam://facts/<company>/<NN-trunk>/leaves/...`；正文脚注坐标真实命中、数字命中；发布后 `fact_leaf_links` 由 B1 正确写入一条（验证最终结果即可，不在 build 层双写）。
   - 脚注坐标在净稿中不存在 → 拒收，且无叶、无 link 残留。
   - 数字在原文/所引 fact 中都找不到 → 拒收（union）。
   - fact 属于别的 trunk → 拒收。
   - source_files 含非本公司 sha → 拒收。
   - 冲突件 `build_conflict` 落 `sam://facts/<company>/待办与冲突/...`，frontmatter target_trunk/target_leaf/metric 正确。
   - 回滚用例：构造一个能通过 LeafBuildService 预检、但 B1 发布阶段失败的输入（如发布瞬间 fact 被标记 superseded），断言发布整体失败后无半成品叶、无残留 fact_leaf_links、无残留 leaf_revisions（验证 B1 同事务回滚在装配路径下仍成立；LeafBuildService 不另开事务写 link）。
4. **关闭态回归**：不置开关时，现有 facts/原子卡/模块 brief/guide/向量表输出与 B1 完全一致（复用 B1 已有的字节快照断言思路，至少断言新代码在关闭路径不被 import 触发写库）。

## 4. 验收硬标准

- 新增测试全绿；全量 `python -m pytest -q` 在我环境基线 **1218 passed / 1 skipped** 之上只增不减、0 failed（/tmp/sam-m5 材料我已就位）。
- `python -m compileall -q app scripts` 通过；`git diff --check` 通过。
- 关闭态：默认无任何新写库/新投影；现有 URI 与产物字节不变。
- 后端代码无行业词/关键词归类/指标枚举；grep 不出现"纳米/涂膜/前五大"等业务词（测试夹具字符串除外）。
- 未接主流水线、未改 harness、未 push/部署、零新依赖。
- 停手并交 `docs/handoffs/facts-tree-segment-b2a-report.md`：文件清单、关键决策、测试命令与结果、关闭态证据、未做项。
- commit 若遇只读沙箱：保留改动并在报告写明，由饭团侧提交，不要绕过权限。
