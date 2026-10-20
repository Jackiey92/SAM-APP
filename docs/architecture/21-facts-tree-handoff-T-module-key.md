# 事实树交接 · 段T：事实必须归属树枝（module_key 主链修复）+ 叶子手册真机补强

- 日期：2026-10-14
- 性质：员工契约缺口修复（归类判断仍由员工做，后端只列合法选项+校验透传），不是护栏调整
- 起点：`467b40a`（段R+S 已提交：join 同事务原子、门开 stale 不自动重建/不复活原子卡）
- 上一段：`20-facts-tree-handoff-S-stale.md`
- 约束：默认关；**不放松任何出处/数字/坐标护栏**；只本地 commit，不 push/部署；本棒不调真模型（饭团真机复跑）

> 执行核对与裁决（饭团WSL，2026-10-14）：下文关于“既有 validated_module 对非法值抛 ValueError”的现状描述不准确，实际共享函数对未知值返回 None。本棒在 DirectReadService 门开候选校验路径显式拒绝非法非 null 值，合法 code/null 保留，关闭态与共享函数语义不变。跨树枝拒绝的错误文字以实际调用路径为准，不为匹配本单文字修改护栏。详见段 T 回执。

---

## 0. 本棒只解决一件事：为什么真机 17 片叶子全失败

### 真机证据（第五次 accept_b3c，隔离 run `/tmp/sam-b3c-real-vvc8z60l`，2026-10-14）

- 五相真模型：classify 1 / attribute 1 / extract 11 / plan 1 / leaf 17
- SQLite 事实 207 条；plan 完全正确（17 叶 + 研发占比冲突 19.51% vs 14.86% 精准识别）
- **段R+S 全部生效**：无 database locked；`m1_documents` 原子卡投影 0；段S 真机回归 `search_does_not_revive_atomic` = true（员工检索后原子卡零复活）
- 但 `built=0 / failed=17 / empty=0`，一片都没建成

### 直接根因（已用真机 trace + 读库坐实）

1. `SELECT module_key,count(*) FROM facts GROUP BY 1` → **全部 NULL（207 条，无一归属树枝）**。
2. `LeafPlanningService.build_one` 用 `list_verified_facts(..., module_key=trunk_code)` 只取本树枝事实（正确行为）→ 每片叶子喂给员工的 `facts` 数实测 **= 0**（trace `0019`/`0025`/`0028` 等 leaf 调用 user 消息均为 `"facts":[]`）。
3. 员工没有本树枝事实 id：`fact_ids` 全返回 `[]`；`## 数据明细` 行 fact_id 只能乱填 `-` / `F1`。
4. 宿主护栏正确拦下：
   - 表格 `fact_id='-'` → `table fact id missing`
   - `fact_id='F1'` 且空 facts → `table fact not found for this leaf`
   - 正文/表格数字对不上（此时无 fact 可对，quote 也不精确）→ `number without fact or quote source`
   - 另含手册问题：`footnote [^2] used without a matching definition`、`table is only allowed under 数据明细`。

**结论：护栏没误伤，是上游提取员工根本没产出"树枝归属"，导致下游叶子无米下锅。**

### 后端链路其实已全部就位（不要重写，只需打通员工产出）

- 员工契约 → 宿主：`DirectReadService._verify`（app/document_read/service.py `_verify`）把 item 透传进 `verify_candidate`。
- `verify_candidate`（app/memory/company_facts/l2_fact_verifier.py:41）已 `module_key=validated_module(candidate.get("module_key"))`。
- `validated_module`（同文件 86-88）：None→None，合法 code→code，非法→ValueError。
- `ExtractedFact.module_key`（l2_fact_extractor.py:25）已存在；`store_facts` INSERT facts.module_key（service.py 175-177）已存在。
- **唯一缺口：file-processor 技能的 extract 输出契约从不要求 module_key，宿主 extract 调用也没给选项清单 → 员工自然不产出 → 全 NULL。**

---

## 1. 必改（按此实现，不扩大范围）

### 1.1 file-processor 技能：extract 每条事实必须带 module_key（主修复）

文件：`skills/file-processor/SKILL.md`

- 在 extract（事实交付）输出契约中，为每条 fact 增加字段 `module_key`。
- 明确这是**员工判断**：把这条事实归到它所描述的树枝。给出 10 个合法 code（来自 app/module_tree.py，确定性清单，非业务推断）：

  | code | 树枝 | 隐藏 |
  |---|---|---|
  | info | 企业基本信息 | |
  | bizov | 业务概览 | |
  | hr | 人员管理 | |
  | prod | 生产 | hidden |
  | purch | 采购 | hidden |
  | sales | 销售 | hidden |
  | mkt | 市场 | |
  | fin | 财务 | hidden |
  | fund | 融资 | hidden |
  | legal | 法务合规 | |

- 归类原则（员工判断，给指引不给死规则）：按事实"描述的对象"归，不按出处段落或文件名；一条事实只归一个最贴切树枝；**确实无法判断时给 null（进"待归类"），不许猜、不许硬塞**。
- 给正例（如"研发人员占比 14.86%"→hr；"纳米级涂膜技术"→bizov）与反例（按文件名归、编造不在清单的 code、把不确定的硬归 info）。
- 不改动 classify/attribute/brief/guide 的契约；本字段只在 extract 事实项出现。
- 按现有技能版本规矩 bump file-processor 的 SKILL version，并官方重生成 catalog（catalog.json + 对应 catalog 测试）；rejected 必须仍为 []。

### 1.2 宿主 extract 调用注入合法树枝选项（确定性数据，非业务判断）

文件：app/document_read/service.py

- 在门开（SAM_FACTS_TREE）extract 相的 `_call('extract', ...)` payload 中，加入来自 `app/module_tree.py` 的树枝清单（建议结构 `[{key, directory, hidden}]`，可复用 plan 相已构造的 MODULE_KEYS/目录清单，保持单一数据源），让员工有明确可选项。
- 关闭态（SAM_FACTS_TREE=False）不注入、行为逐字节不变。
- `_verify` 已透传整个 item，**确认 module_key 不被 `delivered` 投影或任何白名单字段过滤丢掉**（当前 `delivered` 只投影 6 字段用于去重提示，不影响候选本体，核实即可，不改语义）。
- 不新增任何后端归类/关键词/映射逻辑；后端仍只做 `validated_module` 白名单校验（已有）。非法 code 现在就会 ValueError 拒绝，保持。

### 1.3 fact-leaf 技能：补 4 类真机写法（手册补强，不松护栏）

文件：`skills/fact-leaf/SKILL.md`（现 0.3.0）

1. **脚注定义必须齐全**：正文出现的每个 `[^n]`，文末都必须有且仅有一条 `[^n]: 原件名 → 坐标`；多来源句子的每个脚注都要定义。给"用了 [^2] 却没定义"的反例。
2. **多来源就配多个脚注**：一句话/一个数据若由多个原件或多个坐标支撑，写多个脚注，不许只挂一个、其余留空。
3. **数据明细表只允许出现在 `## 数据明细`**：`## 正文` 内禁止 markdown 管道表；需要表格的数据一律进数据明细（n 型可省略数据明细，但一旦给表就必须在该章节内）。
4. **表内数字必须与 fact 逐字一致**：填了 fact_id 的行，数值/单位/期间必须与该 fact 的 value/unit/period 对得上（numeric_guard）；正文数字要么来自 fact，要么与脚注引用块里的数字 token 逐字相同。给"四舍五入改写 2600.00→2600 导致漂移"的反例。
- bump fact-leaf version（0.3.0→0.4.0），官方重生成 catalog；rejected=[]。

---

## 2. 测试（假 worker / 离线，不起本地网络）

新增/扩展（沿用现有 fake worker 风格，关闭态测试一律不动）：

1. **主链贯通**：门开 extract 员工返回的 fact 带 `module_key='bizov'` → 落库 `facts.module_key='bizov'`；返回非法 code → 该候选被拒（ValueError 路径）；返回 null → 落库 NULL 不报错。
2. **叶子只收本树枝**：构造 bizov 2 条、hr 1 条事实，build bizov 叶时喂给员工的 facts 恰好 2 条且全 bizov；数据明细行只能引用这 2 条 id，引用 hr 的 id → `table fact not found for this leaf`。
3. **端到端假数据**：门开 extract 带 module_key → plan → leaf 成功 publish（账本+投影同事务，沿用段R 断言），built=1、failed=0。
4. file-processor / fact-leaf 技能 catalog：版本号、技能数、rejected=[] 断言更新。
5. 不新增对关闭态的任何期望变化；关闭态 golden 逐字节保留。

## 3. 非目标 / 禁止

- 不改任何护栏的判定严格度（坐标、脚注位置、数字 union、fact_id 表内外一致、悬空/未用脚注）。
- 后端不出现任何树枝关键词/正则/推断；选项清单只能来自 module_tree 静态定义。
- 不把 module_key 设为强制非空：null（待归类）是合法员工产出；叶子只消费已归属事实。
- 不碰触发器、schema、迁移、投影通道（段R+S 已验）。
- 不解决"冲突件成件"（那是段Q2，单独一棒）。
- 不调真模型、不 commit/push/部署；compileall + diff-check 后停手，报告 `docs/handoffs/facts-tree-segment-t-report.md`。

## 4. 饭团真机复跑判据（第六次 accept_b3c，CRIS 不需跑）

在 1.1/1.2/1.3 完成、全量 0 failed 后：

- extract 落库事实 `module_key IS NOT NULL` 占比应显著为正（允许少量 NULL=真不确定）；
- leaf 调用喂给员工的本树枝 facts 数 > 0；
- `built` 明显上升、`failed` 大幅下降；"纳米级涂膜技术，提升产品精度"成段进某 bizov 叶（nano_prose_in_leaf=true）；脚注坐标 Dossier.read 可回查；
- 段S 回归仍 true（检索零复活）；零原子卡投影；trunk/tree brief-guide 生成；原件 sha 前后一致。
- 剩余失败若仍为个别员工写法问题，按真实报错再决定是否继续补手册，**不靠放松护栏冲通过率**。
