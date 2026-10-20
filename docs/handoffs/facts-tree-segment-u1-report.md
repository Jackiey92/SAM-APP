# U1 文件驱动主链

## 开工接口/升级影响（实现前记录）

- 受控循环沿用 plan/leaf 预算相，以 protocol=mapped-md-v2 + operation=read_assign/compare_submit 区分。首次只给元数据、坐标目录、模板与单个有界窗口；逐叶比较仅给分配证据及现有叶。无新员工工具/registry入口。
- 映射MD窗口授予固定文件/映射指纹、真实坐标/行范围；大型表保留表头分窗，超长不可安全窗口保留未解决，不伪造坐标。
- 新正文schema_version=2，仅内容/索引。结构块与证据ref一一关联；修订沿用leaf_revisions，新增门后操作回执/结构块/待办表，append-only审计及消费通知。不修改全局schema版本，不自动升级真实数据，首次进入U1的显式门开写路径才装配辅助表。
- 基础version+content_sha在IMMEDIATE内比较；投影沿R同连接join，操作回执/关联/历史/待办同事务。幂等operation_id+request_sha。待办双方结构证据及责任任务保留，不裁决。
- 旧叶可读并明确legacy标记，本棒不自动迁移旧章节；旧格式升级写拒绝并给明确未解决，防偷偷改名总结。L1/L0先接消费通知；_active适配v2识别，不隐藏模型总结。

执行结果、测试数字及限制待收尾补记；当前不代表完成。

## 实现收尾（以下覆盖开工草案中的细节）

起点 HEAD a1dfe7b；未commit/stage/push/部署、未调真模型、未开多员工或并发长测。完整读22号单、绝对路径讨论稿、段T WSL验收回执及指定现有代码。沙箱本轮可正常执行既有pytest隔离夹具，没有绕过conftest/写保护。

### 文件清单

- 新增 app/facts_tree/md_reader.py：MDWindows、固定指纹授予、单员工受控read_assign/compare_submit循环与基础恢复。
- 新增 app/facts_tree/md_publish.py：MDLeafPublisher、schema v2装配、块/行索引校验、版本CAS、待办、幂等操作回执与审计。
- 修改 app/document_read/service.py：门开默认调用_run_mapped_leaves，经现有_call计费/传输；关闭态分支不动；原_run_leaves保留为显式v1兼容API，不再是新任务默认。
- 修改 app/facts_tree/rollup.py：_active认v2内容；显式rollup可机械复制内容/索引、标题链接，不要求概览、不生成新总结。新主链不自动跑模型总结，只发消费通知。
- 修改 skills/fact-leaf/SKILL.md：0.4.1→0.5.0，新增优先的mapped-md-v2协议；v1规则明确只供无新协议的兼容调用。
- 官方重生成 harness-openclaw/skill-registry/catalog.json；tests/test_phase1_catalog.py更新版本断言。
- 新增 tests/test_facts_tree_mapped_md.py（返工后35实例）。
- tests/test_document_read_leaves.py的open_task及tests/test_facts_tree_module_delivery.py的原v1端到端用例：显式调用旧_run_leaves兼容路径。原断言/测试没有删或降低；这些用例不再被声称覆盖U1默认主链，后者由新增真实provider测试覆盖。test_document_read.py/关闭golden不改。
- 本报告。

### 短结构与接口

1. MDWindows默认每窗≤80行/10000字符。按实际document_map元素顺序，段落受控行窗口；管道表窗口保留原两行表头与原始行号/坐标。超长单行不能安全授予则记录unresolved；未映射的非空MD行列出unresolved，不伪造出处/页码。无原件/二进制/全库阅读接口。
2. plan/read_assign每次只给一个window、至多256项目录元数据、模板位置及已读窗口元数据；员工返回leaf_code+ranges分配，必须在当前窗口范围内。至多请求两个已读context_windows用于明确上下文，不开放未来窗口或其它文件。分配在leaf调用前持久化。
3. leaf/compare_submit给已分配授予、本目标当前正文/结构索引/version/SHA及仅本树枝、同源、quote确在授予范围内的facts。既有叶比较视图超过64KB则明确unresolved，不无限扩大上下文。员工自行选材、归类、判断冲突。
4. blocks={block_id,text,evidence[]}；evidence包括ref/grant_id/file_hash/original_name/source_loc/start_line/end_line/fact_ids。文本只装配内容/索引，所有段落和表格数据行各自使用引用。每段/行数字仅查本段/行引用的事实/原文，表头数字也须被各行证据支撑；真实坐标仍需resolver命中。索引明确关联block与范围、源名/指纹、事实ID。宿主不补写业务文字。
5. submit携带operation_id、base_version、base_sha、完整保留旧blocks+员工选的新块、conflicts、note。IMMEDIATE内核对最新version/SHA；BaseChanged拒绝旧稿并废弃旧比较draft，需重新读当前内容/比较。不会持写锁调模型。
6. conflicts={old_block_id,new_block,reason}：新候选不能同时进入正常blocks；旧内容、旧证据、候选内容/证据、基础版本、责任job、理由全部留存pending。无冲突新增可与待办同批提交；后端不判冲突、不裁决。
7. 当前v1叶可读完整旧正文/脚注/事实关联。禁止自动把旧概览改名内容：普通升级写返回明确未解决。允许legacy位置的conflict-only留证，不更新原v1叶，不新增正常叶修订。
8. 最终完成由每个映射窗口/分配的处理回执核对，不采信员工complete。coverage_complete与content_complete分开，pending_count说明待办/未解决项；workflow done不宣称内容严格验收通过。旧窗口/操作重放不重复历史/待办/正文。

### 持久化与兼容影响

仅显式进入门开U1写入口后建立门后辅助表（MDLeafPublisher.require_enabled/_require_writable在DDL之前）。全局schema版本/既有迁移/触发器不改；不扫描、转写或迁移旧叶或真实公司数据。这里是首次U1使用的建表装配，不是自动真实旧数据转换；本轮只在隔离库执行。云只读/关闭态不会装配它。

- md_leaf_bodies(company_id,leaf_code,blocks)：结构内容/证据索引，正文frontmatter blocks_sha防侧表与投影不一致。
- md_leaf_operations(company_id,operation_id,request_sha,receipt,created_at)：幂等成功回执；SQL触发器禁止UPDATE/DELETE，append-only。receipt含job_id、owner_skill、note、映射指纹、leaf/version/待办数。
- md_leaf_todos(company_id,operation_id,ordinal,leaf_code,base_version,payload,status)：双方证据及员工理由，status默认pending；没有裁决API。
- md_leaf_notifications(company_id,operation_id,leaf_code,version)：L1/L0受影响通知，U1不调用总结模型。
- 沿用leaf_revisions/fact_leaf_links/facts_tree_leaf_index；内容、结构索引、关联、修订、操作回执、待办、通知与m1_documents/m1_chunks同事务。

仍走段R的store._publish_documents→真provider join接缝，provider/projection源码不改，段S无原子卡复活逻辑不改。model_tool仍为单次传输；受控循环在宿主，沿plan/leaf预算相，不新增员工工具或registry。每次传输前_call持久计费；总phase预算有限、deadline30分钟、单次180秒。中断保留已读/分配/draft/调用次数；不宣称外部模型网络exactly-once。

### 实际离线验证

首轮U1专项：11 passed / 4 failed，2.30s。失败完整栈在工具输出中；新fixture把表格裸9写成9个，原数字护栏正确拒绝。仅修测试逐字引用，不松护栏。
修正后早期相关回归139 passed，12.41s；首轮全量1460 passed/1 skipped，162.11s。
补齐边界后相关回归170 passed，14.09s；U1专项23 passed，3.64s（最终审计回执字段补齐后重跑）。后续全量收尾见本报告末。

U1专项对应：
- test_two_windows_two_leaves_real_provider_and_coverage：两个窗口分别两叶，真实provider读回/索引、历史/操作/通知各2、无概览/历史第三部分、_active兼容、已完任务不调用模型重放。
- test_bad_evidence_zero_side_effects（8）：数字/哈希/源名/坐标/行范围/未读grant/非法fact/无引用均零叶账本/投影/待办/回执。
- test_revision_append_replay_and_stale_base：v1→v2连续修订、重放不重复、旧基础拒绝、审计SQL不可重写。
- test_conflict_saves_both_evidence_keeps_original_allows_addition：双方证据留待办、旧叶字节不变、重放不重复、无冲突新增同时可写。
- test_projection_failure_rolls_back_todo_revision_and_receipt：真实embedding投影故障整体回滚，包含事实关联与操作回执。
- test_read_interrupted_resume_and_changed_fingerprint：读后未提交中断恢复，指纹变化拒旧游标。
- test_table_windows_preserve_header_and_bounds：大表分窗保表头/原坐标、有界。
- test_default_gate_pipeline_uses_window_actions_not_old_leaf_loop（4）：新默认主链与预算partial重构reader续跑，首轮模型actions为classify/attribute/extract/plan/leaf/plan/leaf（违反上游分窗，本轮已替换），不重提取或重建成功叶，无旧brief/guide。
- test_old_format_read_and_conflict_only_no_auto_migration：v1读兼容，禁止隐式升级，双方原始证据待办、正常旧叶不动。
- test_real_content_table_row_has_specific_index_and_number_guard：内容表行可回查，坏行数字不落库。
- test_no_content_and_unresolved_coverage_are_distinct：无适用内容/未解决明确区分，零发叶，覆盖完成≠内容完整。
- test_lost_submit_receipt_resume_replays_without_new_model_call：提交后回执中断，从持久draft恢复，操作回执重放不重调已成功叶、不重复历史。
- test_context_regrant_only_prior_read_windows：明确申请已读上下文，新增内容证据仍来自授权窗口。
- test_gate_disabled_does_not_create_extension_schema：门关入口拒绝，辅助表零创建。
- test_fact_from_same_source_but_unread_coordinate_rejected：同文件别的未授权坐标事实不可借用。

### 限制/未做项（U2/U3边界）

U1单员工，未实现跨进程任务租约/调度/多员工；已有SQL基础version/SHA约束，但本轮不声称真实并发黑盒通过。U1普通更新保留旧块原文/索引，只追加员工选定的新块；任意旧块重写/迁移需显式后续流程，避免无声丢证据。超大单行/未映射非空行/过大旧叶比较视图明确未解决，不自动修复。新model请求跨网络的不确定重试可能再次计费，数据库已成功操作可幂等重放。

不跑真模型、不进入U2/U3、没有本棒真机通过结论；段T未通过不被本轮离线数字覆盖。来源只用真实映射行/坐标，没有自造页码。没有读/改真实库/原件、没有读取打印凭证；harness DREAMS/dreaming/session-corpus等既有脏文件不动、不stage。catalog是本棒正式产物。


### 最后自审补齐

- 门开extract确认为无事实（facts=[]、complete=true且无拒绝）时，允许进入独立MD覆盖/归叶循环，支持只有可引用描述的文件；不伪造SQLite事实。全坏批次仍保留原complete=False/反馈重试直到partial，关闭态完全保留原分支。新增主链4参数分别验证有/无事实与预算续跑，26个U1实例包含定性叙述/数字/表格结构。
- 新增test_projection_failure_with_todo_and_addition_preserves_old_atomic_unit：在旧叶上同批增加正常内容和待办，投影抛错后九张表逐行快照完全不变，旧叶字节保留。
- 修订回执明确job_id/owner_skill/note/映射MD指纹，独立审计可直接回查责任任务；映射元数据/version变化也包含在整个Dossier指纹检查内，不沿用旧游标。
- 最新U1专项26 passed，4.25s；无事实主链及v1模块/关闭态回归105 passed，11.61s。最新全量待末尾追加真实收尾数字。

## 默认入口上游返工（本节取代早期默认入口说明）

饭团独立探测正确：首轮先classify整稿，末端分窗不能满足22号单。早期动作序列和全量数字仅为历史，不是当前验收证据。

- run在构造全文context之前进入mapped-md-v2。每窗classify/attribute/extract/plan/leaf均经_call先持久计费；只授予当前窗口/行范围。分类、实体归属、选材由员工完成；完整v1链仅run_legacy显式调用。关闭态原链及golden不动。
- 每窗preparation持久化分类、归属、反馈、事实与完成状态。分配和提交回执保留，恢复不重发成功窗口。提取仍复用原候选出处/实体/数字/module校验和SQLite正本提交。
- source_span附精确ranges与mapped_fingerprint，不改schema。后续可用facts必须被授予行范围证明；无定位的历史事实仅整个坐标授予后可用，防止同表重复quote泄漏未来行；不迁移历史事实。
- 坏候选原文只留审计，重试反馈仅原因/序号/当前坐标；already_delivered仅本窗口约定字段，避免将未读内容经反馈或事实透传。
- file-processor升级1.2.0，补上游窗口协议；fact-leaf本棒0.5.0不再改。官方重生成catalog，9技能、rejected=[]；tool-registry不变。

新增默认入口测试：
- test_default_three_window_resume_recursive_requests_no_full_md_no_unread_facts：真实provider、3窗口；递归检查每次请求无整稿/未读第三窗文本或事实。两窗预算partial后重构reader续跑，精确15次调用（五相各3次），成功窗口不重发，调用前账单已持久，3个操作回执/修订。
- test_same_coordinate_partial_window_cannot_leak_future_fact_even_quote_repeats：同表同坐标3窗且quote重复，带范围未来事实和模糊旧事实不提前泄漏；授予第三窗后仅精确证据可用。
- test_window_fact_retry_never_echoes_rejected_ungranted_quote：员工整稿quote被原护栏拒绝；下一请求递归不含整稿，坏原文留审计，后续合法候选完成。

原.runtime探测未修改；它未注册绑定且memory=None，不冒称原脚本原样通过。等价默认入口探测用已注册真实provider隔离测试覆盖。

实际：返工相关四文件108 passed（11.46s）；最新U1专项35 passed（5.47s）。静态检查与全量收尾另列。报告编辑首次用python命令失败（command not found），改用现有工具完成，不影响产品。未stage/commit/push/deploy；未调真模型；未改harness运行文件。段T与本棒没有真机通过结论。

### 本轮返工最终收尾

- 全量 `.venv/bin/python -m pytest -q`：**1480 passed / 1 skipped / 0 failed / 0 error**，165.79s。日志 `/tmp/sam-u1-upstream-pytest.log`。较1445基线新增35个测试实例。
- `.venv/bin/python -m compileall -q app tests scripts`：exit 0。
- `git diff --check`：exit 0。
- `git diff --name-only -- app/memory app/db tests/test_document_read.py tests/fixtures/document_read_closed_b2b1.json harness-openclaw/tool-registry` 空输出；provider/R/S、schema、关闭态原测试/golden、tool-registry无修改。
- HEAD仍a1dfe7b，无本棒提交。git status中的harness DREAMS及memory运行脏文件未修改/未stage；catalog为此前本棒协议升级的正式产物，非运行文件。未push/deploy，未真模型，停在U1等待饭团黑盒。

## 事实提交/窗口检查点原子性返工

饭团故障注入属实：此前事务中原地更新prepared/state，SQL回滚后_finish再次保存会产生幽灵fact_ids和complete。该缺陷不是数字/业务护栏问题。

改动仅本问题：
- app/document_read/service.py::_prepare_md_window：候选提交前deepcopy检查点；在事务内将事实、accepted/feedback/fact_ids、窗口complete、parse_jobs字段与副本检查点一并提交。活跃state/record在SQLite上下文成功退出（commit完成）前不变；成功后只回填已提交字段，保留reader持有的record/prepared引用及已计费账单。
- 任何候选部分写入、_save或commit失败沿原异常路径回滚/向上抛；_finish只会保存旧事实进度及已计费调用。没有吞异常、清空进度或放宽守卫。
- commit成功后后续save失败，活跃及事务内持久检查点都已complete；重构reader将跳过该窗口的成功提取。原分配/操作幂等恢复机制保留，不改R/S或叶发布事务。
- tests/test_facts_tree_mapped_md.py新增test_default_fact_checkpoint_failure_atomic_resume四参数：save（检查点写后抛错）、commit（提交前回滚抛错）、candidate（真实候选写后抛错）、after_commit（提交后外部回执save抛错）。均走默认完整入口、真实provider/隔离SQLite；检查持久和活跃状态、事实ID/accepted/complete/反馈、parse_jobs、计费保留，并重构reader验证重提取或跳过已提交提取、2叶/2历史/2操作且零原子卡。

本轮实际验证（不能用前次1480全绿替代）：
1. 新故障专项首次4 failed / 35 deselected，0.47s：新测试误写SELECT fact_id FROM facts，实际列为id；全部同一夹具错误，已仅修测试SQL。完整错误：sqlite3.OperationalError: no such column: fact_id（tests/test_facts_tree_mapped_md.py:481）。
2. 修正后尝试专项，pytest未收集（exit 4）：tests/conftest.py:34 `with socket.socket() as listener` → /usr/lib/python3.14/socket.py:236 → PermissionError: [Errno 1] Operation not permitted。不重试/不绕过隔离，新增4例及相关回归待饭团正常WSL执行。未运行本轮全量；前次1480数字仅适用于本次修复前。
3. `.venv/bin/python -m compileall -q app tests scripts` exit 0；`git diff --check` exit 0。本次只追加上述service修复、4实例与报告；关闭态/golden、provider/R/S、schema、catalog、harness运行文件无本次修改。

HEAD仍a1dfe7b；未stage/commit/push/deploy、未调真模型。停手等待饭团回归及验收。

## 最后故障测试识别修正

饭团正常WSL此前专项35 passed / 4 failed（6.74s，日志 `/tmp/sam-u1-atomic-targeted-rerun.log`）：四例已通过回滚/恢复/计费/历史检查，仅原子卡识别断言误判。SQLite LIKE不区分大小写，`%/F%.md`会匹配`/facts/`，错误计入两片正常叶（fact_json=NULL）。

本次只修改tests/test_facts_tree_mapped_md.py末条断言为`m1_documents WHERE fact_json IS NOT NULL`计数为0，沿用既有原子文档类型判据；两叶、两修订、两操作及恢复断言完整保留。未改provider/业务代码/护栏/隔离机制。

本次实际检查：
- 四例故障专项：4 passed / 35 deselected，1.27s。
- 整个U1专项：39 passed，6.67s，0 failed。
- `.venv/bin/python -m compileall -q app tests scripts`：exit 0。
- `git diff --check`：exit 0。
- 未运行全量，未调真模型；仅修测试识别及本报告，未碰harness运行文件，未stage/commit/push/deploy。停在U1等待饭团验收，不进入U2。
