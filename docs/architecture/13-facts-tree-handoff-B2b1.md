# 事实树 B2b1 交接详档：文件→叶子编排器（确定性内核，假员工可测）

给 CRIS。本棒在 B2a（`84be127`，已黑盒通过）之上做 **B2b1**。权威设计 `docs/architecture/09-facts-tree-design.md` v1.2（重点 §8 文件驱动五步、§9 显隐）；B1/B2a 详档 `11`/`12`。本档是 B2b1 唯一施工口径。

## 0. 铁律（同前）

- 只本地 commit，不 push、不部署；只改本仓，不 stage/提交 `harness-openclaw/` 下既有无关脏文件。
- 零新增第三方依赖；后端零业务规则（不得出现行业词/关键词归类/指标枚举/写死公式；叶子选择只能来自员工返回值，后端只做合法性校验）。
- 业务判断只能来自员工；后端只做形状/出处/坐标/数字/引用完整性护栏。
- 只用测试库（`tmp_path`）；不删原件、不删 facts 正本、不改既有投影行为。
- 本棒**默认开关关闭**，**不接入 document_read 主流水线**（那是 B2b2）、不调用真实模型（worker 可注入，测试用假 worker）。
- 遇设计/代码口径冲突：停手在最后消息列点，不自行解释。

## 0.1 以现有代码为准（已核对，直接照用）

- 叶子发布：`app/facts_tree/leaf_store.py:LeafPublishService`，`publish_leaf/publish_conflict`。
- 宿主装配：`app/facts_tree/leaf_build.py:LeafBuildService(leaf_store, *, quote_resolver)`，方法 `build_leaf(*, company_id, leaf_code, employee_payload, job_id=None)`、`build_conflict(**kwargs)`；员工 payload 形状由 `leaf_contract.parse_employee_leaf` 约束（leaf_code/markdown/source_files/fact_ids/note，markdown 只含正文段）。
- 取件器：`app/facts_tree/quote_sources.py:DossierQuoteResolver(documents)`，`documents` 为 `{file_hash: document}`，document 含 `markdown` 与 `document_map.elements`。
- 模板：`template_packs.load_pack(name).leaves`，每项 `.code/.trunk/.shape/.title`；绑定 `company_binding.CompanyBindingStore.get(company_id)`。
- 汇总：`rollup.rollup_trunk(*, leaf_store, company_id, trunk)`、`rollup_tree(*, leaf_store, company_id)`。
- 受控净稿：`app/document_read/dossier.py:Dossier.read(source_loc)`、`.elements/.lines/.file_hash`。
- 员工运行时：`app/employees/worker.py:EmployeeRunner.run(*, skill, text, company_id, thread_id=None, ...)`，返回 `EmployeeResult`（list）。参考 `l1_brief.py` 的"注入 worker、JSON 进 JSON 出、后端校验"模式。
- 事实正本：`facts` 表真实列（临时库 PRAGMA 实测）= `id,company_id,entity,attribute,period,value,value_type,unit,source_file,source_page,source_span,valid_from,valid_to,confidence,status,confirm_mode,superseded_by,created_at,module_key`（module_key 由 `app/db/database.py:184` 迁移 ADD COLUMN，建表 SQL 里没有，别被建表语句误导）。**优先复用** `ConsolidationService.list_verified_facts(*, company_id)`（`l2_consolidation.py:223`，已带 `WHERE status='verified' AND superseded_by IS NULL AND valid_to IS NULL`，返回 dict 含 module_key），再在内存按 `module_key==trunk` 过滤；不要另写一套 SQL。fact **没有 source_loc 字段**，出处是 `source_file`(file_hash)+`source_page`+`source_span`。`source_files` 列 = `file_hash,original_name,mime_type,size_bytes,storage_path,origin_zone,status,uploaded_by,uploaded_at`（无 company_id，归属靠 facts/file_classifications）。
- 员工返回：`worker.run(...)` 返回 `EmployeeResult`（是个 list，见 `worker.py:24`），每个元素是一个 payload dict，消费方式照搬 `l1_brief.py:111` 起（空 list = 员工无结论，要当 unresolved 处理，不得静默当成功）。
- 净稿片段文本与坐标：只能经 `Dossier` 真实 API 取（`.read(source_loc)`/`.elements`/`.lines`/`.file_hash`，见 `dossier.py`）；document dict 的键以 `dossier.py` 实际读取为准，先读它，不要臆造键名。
- 模块树枝：`app/module_tree.py:MODULES`（key/directory/order/hidden）。

## 1. 目标与非目标

**目标：一个确定性编排器 `LeafPlanningService`（建议新文件 `app/facts_tree/leaf_plan.py`）**，对"一家公司 + 一份已解析净稿文档（可含多 file_hash 的授予集合）"执行文件驱动：

1. **规划（员工判叶）**：把该文档净稿 + 该公司绑定模板包的叶子目录（只给 code/trunk/title/shape，不给业务规则）交给员工 `fact-leaf` 的 **plan 动作**，员工返回"本文件涉及哪些叶子"的 `leaf_code` 列表（可含本文件没有原子事实、但有成段描述的叶子——这是修复"纳米级涂膜"类描述漏失的关键）。
2. **逐叶构建**：对每个叶子，组装该叶输入（该文档净稿中相关坐标片段 + 本公司该 trunk 当前可用 verified facts 的精简清单，只给 id/entity/attribute/period/value/unit/source 坐标），调员工 `fact-leaf` 的 **leaf 动作**产出 `parse_employee_leaf` 形状的 payload，再交 `LeafBuildService.build_leaf` 落盘（复用其全部护栏与宿主装配）。
3. **汇总**：所有涉及叶子处理成功后，对受影响 trunk 调 `rollup_trunk`，最后调一次 `rollup_tree`（幂等；无内容叶子不产生文件，天然显隐）。
4. 全程可注入 worker（测试用确定性假 worker），默认关闭，不接主流水线、不调真模型。

**非目标（B2b2/B3 才做）**：不接入 DirectReadService.run、不做预算/断点续跑状态机、不做多文件合并调度（本棒只处理"一次调用传入的授予文档集合"，重复调用的幂等由 B1 no-op 与 leaf_revisions 保证）、不做开关默认打开、不做建档 UI、不新增插件工具、不改 harness 运行时。

## 2. 施工要求

### 2.1 员工协议（在现有 `skills/fact-leaf/SKILL.md` 内扩展两种动作，不新增第二个技能）
- 用输入 JSON 的字段区分动作：
  - **plan**：输入 `{"action":"plan","company_id":...,"template":{"trunk_code","trunk_directory","leaf_code","title","shape"}[],"documents":[{"file_hash","original_name","source_loc","md"}...]}`。员工只返回 `{"leaves":[{"trunk":"bizov","leaf_code":"L-bizov-07"}, ...]}`，可附 `"conflicts":[{"trunk","leaf_code","metric","reason"}]`（本棒 conflicts 只记录不自动落冲突件，落冲突件仍走 build_conflict，由 B2b2 串）。
  - **leaf**：输入 `{"action":"leaf","company_id":...,"target":{"trunk","leaf_code","title","shape"},"fragments":[{"file_hash","original_name","source_loc","md"}...],"facts":[{fact_id,entity,attribute,period,value,unit,source_loc}]}`。员工返回 B2a 已定的单对象 payload（leaf_code/markdown/source_files/fact_ids/note），markdown 只含正文段。
- SKILL.md 里写清：plan 必须通读 fragments，**描述类/定性信息（无数字的技术、工艺、模式描述）也要归叶**，不受"是否抽到原子事实"影响；只从给定 template 里选 code，不得自造叶子；无任何匹配可返回空 leaves。
- 仍保持 frontmatter `roles: []`、`name=fact-leaf`；更新后**必须重跑** `python scripts/build_phase1_catalog.py`，catalog.json 内容除 content_sha256/version（如需 bump 到 0.2.0）外结构不变、rejected 仍 []，tool-registry 零变化。

### 2.2 `LeafPlanningService`（确定性，后端无业务判断）
构造建议：`LeafPlanningService(db_path, memory, *, worker, documents)`，其中：
- `documents` 是本次任务授予的净稿集合（喂给 `DossierQuoteResolver`，与 B2a 同源约束：不得按 company 全库自取，只能用显式授予集合）。
- `worker` 可注入（默认 `EmployeeRunner()`，但本棒不要求真跑）。
- 一个 `LeafPublishService` 实例 + `DossierQuoteResolver(documents)` + `LeafBuildService` 在内部复用。

方法建议：`plan_and_build(*, company_id, job_id=None) -> dict`，流程：
1. `require_enabled()`（关闭即拒，返回与 B2a 一致的禁用异常）。
2. 读绑定模板包；未绑定 → 明确异常（不自动选包，建档是 B3）。
3. 从 `documents` 生成 plan 输入：用 `Dossier.elements` 枚举授予文件的真实 `source_loc`、经 `Dossier.read` 取对应文本组成 fragments（只放授予文件）；模板叶子目录从绑定包生成（只给 code/trunk/title/shape，不给 scope 以外的业务规则）。
4. 调 `worker.run(skill="fact-leaf", text=<plan JSON>, company_id=...)`。`EmployeeResult` 是 list，取其中的 payload（照搬 l1_brief 解包），员工对 plan 动作返回 `{"leaves":[...]}`；空 list 视为员工无结论（见测试 10），不静默成功。**后端校验**：每项 trunk/leaf_code 必须存在于绑定包且互相一致（code 的 trunk 段 == trunk、`LeafCode.trunk` == trunk）；去重；非法项整条拒绝并计入 `rejected_plans`（不影响其它合法叶），不猜测、不补全。
5. 对每个合法叶子：
   - 取本公司该 trunk 的当前 facts：复用 `list_verified_facts(company_id=...)` 后在内存过滤 `module_key == trunk`，精简为 `{fact_id=id, entity, attribute, period, value, unit, source:{file_hash=source_file, source_page, source_span}}`（注意没有 source_loc；员工若要引用坐标，只能用授予文档里真实存在的 `source_loc`，fact 仅作数字凭证）。source_file 的本公司校验仍由 B1 `_sources` 兜底。
   - 片段：用 `Dossier.elements` 枚举授予文档的真实 `source_loc`，经 `Dossier.read(loc)` 取文本，组成 fragments 提供给员工（本棒可提供授予文档的全部 elements 片段，由员工据此写叶；坐标必须原样照抄，不得新造）。
   - 调 worker leaf 动作 → `parse_employee_leaf` 形状校验 → `LeafBuildService.build_leaf(...)`。任何 `LeafRejected`：捕获，记入 `failed_leaves[{leaf_code, reason}]`，**继续处理其它叶**（单片坏不拖垮整批；已成功的叶保留——B1 已落盘即有效事实，不回滚已成功叶），最后在返回里如实报告。
6. 成功处理过至少一个叶的 trunk：逐个 `rollup_trunk`；全部完成后 `rollup_tree` 一次。rollup 失败应抛出并在结果标注（rollup 是机械汇总，失败说明护栏或数据不一致，需暴露）。
7. 返回结构：`{"planned":[leaf_code...], "built":[{leaf_code,uri,version,changed,updated_at}...], "noop":[...], "rejected_plans":[...], "failed_leaves":[...], "rolled_up_trunks":[...], "tree_rolled": bool}`。不得把异常吞成成功。

### 2.3 事务/幂等/边界
- 单叶发布的原子性交给 B1（已测）；编排器**不**跨叶搞大事务（避免一片坏锁死全批），但要保证"报告的成功/失败与实际落盘一致"。
- 同一 documents 同一内容重复调用：依赖 B1 no-op（changed=False）与 rollup 幂等，第二次 built 多为 noop、不产生新版本、不重复写 link（B1 已保证）。测试覆盖。
- 不读原件字节、不提供 objects/图片；仅净稿+坐标。
- worker 抛传输类异常（`WorkerUnavailable`/`EmployeeTransportError`）：向上抛，由未来 B2b2 状态机处理；本棒不要伪装成业务失败。

## 3. 测试（pytest，tmp_path，假 worker，不连真模型）

新增测试文件，用可编排的假 worker（按 action 返回预设 plan/leaf），复用/仿造 B2a 的临时库 kernel 与净稿夹具：
1. plan 合法返回多叶 → 逐叶 build 成功 → 受影响 trunk rollup、tree rollup 各一次；返回结构计数正确；落盘 URI 在各自 `sam://facts/<company>/<NN-trunk>/leaves/`。
2. **描述不漏**：假员工 plan 选中一片 n 型叶（如 L-bizov-07），leaf 产出含"纳米级涂膜技术，提升产品精度。[^x]"且坐标为净稿真实坐标、无 fact_ids——必须成功落 n 型叶（无数据明细表）；坐标改成不存在即被 B2a 坐标预检拒收并计入 failed_leaves。
3. plan 返回非法 leaf_code / trunk 与 code 不一致 / 自造叶子 → 进 rejected_plans，不影响其它合法叶。
4. 某叶 build 抛 LeafRejected（数字无源/跨 trunk/外源）→ failed_leaves 记录，其它已成功叶保留且有效，tree 仍 rollup。
5. worker 传输异常 → 向上抛出，不记为成功。
6. 幂等：连续两次 plan_and_build 同输入，第二次全部 changed=False、leaf_revisions 不增、fact_leaf_links 不重复。
7. 未绑定模板包的公司 → 明确异常。
8. 关闭开关（monkeypatch settings.SAM_FACTS_TREE=False）→ 拒绝且 DB/记忆零写入（沿用 B2a 关闭态断言思路）。
9. 授予集合外的 file_hash 即使被员工引用也取不到块、相关脚注坐标预检失败（不外泄未授权净稿）。
10. 只有 plan 无任何 leaf（员工返回空）→ 不建叶、可不 rollup 或 rollup 幂等空操作，返回 built=[]；不产生空叶文件（显隐）。

## 4. 验收硬标准

- 新增测试全绿；全量在基线 **1269 passed / 1 skipped** 上只增不减、0 failed。
- `python -m compileall -q app scripts`、`git diff --check` 通过。
- 关闭态零写入；不接 document_read 主流水线（grep 确认 service.py 无变化/无 import leaf_plan）；零新依赖。
- 后端无行业词/关键词归类/枚举（测试夹具与 leaves.yaml 既有 scope 文案除外）。
- fact-leaf SKILL.md 更新后 catalog 重生成：rejected=[]、tool-registry 零 diff、phase3 零影响。
- 停手交 `docs/handoffs/facts-tree-segment-b2b1-report.md`（文件清单、协议、假 worker 测试、关闭态证据、未做项）。commit 遇只读沙箱就保留改动、报告写明，由饭团侧提交。
