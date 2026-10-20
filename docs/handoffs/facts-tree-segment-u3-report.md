# 事实树 U3：消费、待办及真实上传入口

## 状态 / 外部证据

按AGENTS、22号单及U3派单实施，当前HEAD a1dfe7b，无本棒提交。已完整读U2独立回执；饭团53专项/87相关回归、32次真实双员工请求是U2背景，不代替本棒终态。真实公司名称拒稿的response已在本机定位（model-trace/f1cd5c111f8845c8a78523b1d837bbb2.response.json）：员工企图用新文件证据替换既有块证据；继续保留`existing content/index must be retained`拒绝，不改业务/出处守卫。没有复制原trace、没有读取凭证或真跑模型。

## 六项收口 / 改动路径

### 1. 机械L1/L0消费与通知

app/facts_tree/rollup.py：v2内容走原_excerpt叶级脚注命名空间，定义中的原件名/坐标/范围不改，两个叶子的[^a]分别变为各自leaf-code-a；v1仍原分支。新增consume_notifications(company_id,fence=None)，只机械拼接内容/索引和标题导航，零模型调用/零总结预算相。

每次消费在BEGIN IMMEDIATE持有短SQL写事务内重新读active叶/修订及投影，按当前版本装配所有树枝/公司简报导览；走原R join同事务投影，并确认对应叶版本。旧进程不能拿锁外缓存覆盖新快照。投影/确认失败整批回滚，通知不删除，下一次仍待消费。新增门后辅助表md_leaf_consumption(company_id,leaf_code,version)，联合主键，version只增不减；md_leaf_notifications仍保留原记录，不做通知丢弃式清扫。

app/document_read/service.py默认窗口完成后消费；已done的mapped任务重放也核对待消费通知，不重复模型或叶历史；消费失败可恢复，不冒称done。app/memory/company_facts/module_projection.py仅门开read接对应trunk-brief/trunk-guide；json读是机械{module_key,layer,content}，不补模型摘要。webapp现有内部确认重建入口门开改消费通知，不再启动旧模块总结员工；门关原路径不变。

### 2. 内容冲突待办真实读/展示

新增app/facts_tree/todos.py，SQLite mode=ro、SELECT-only；缺md表为空。ConsolidationService.list_todos仅门开附加kind=md_content_conflict（旧数字待办kind=fact_conflict），旧表/记录/处理API不删不合并。新待办对外pending语义，兼容列表open筛选；resolved/dismissed不混入pending项。字段含目标叶、位置、基础版本、旧/新正文与双方证据、员工原因、任务及操作关联ID。

webapp/api/todos及待办中心真实读取该列表；md待办必须与宿主SAM_COMPANY_ID匹配，不因query选择别家公司而泄漏新待办。旧数字待办原scope/按钮保持原路径。_todo_card.html新类型独立卡片，双方内容/原件链接/真实坐标/范围/事实ID/责任任务可看，HTML自动转义；没有旧数字resolve按钮，也没有裁决API，string md:... ID不能进入旧int路由。pending不自动选胜出值。todo中心分别展示两种类型，关联保留以便人工核对，不掩盖潜在重复。

cloud_export原调用list_todos即同步新只读视图，没有快照版本变动。app/cloud_home.py仅门开且有合法md项时用同主题待办卡片，只传公司隔离、类型检查后的显示字段；不展开任意page_context，保留旧L2冲突的只读展示，全部操作按钮禁用。门关维持旧云窗口。safe_md_view拒绝跨公司/脏形状，显示字段不信任快照控制变量。

### 3. 实际产品上传接缝

新增app/document_read/upload.py，webapp/app.py::_parse_job_pipeline已真实引用该入口：原件转换→独立QC→QC绑定核对→映射MD员工五相。门开跳过旧EntityBridge/财务全文prepare/全稿归类/旧L2原子投影/旧brief-guide/导入总结员工；原件仅上游解析/QC处理。门关仍旧链。

ParseJobManager现有有界多文件执行器承担网页并行，每逻辑reader一文件；上传seam复用U2 run_files（单任务workers=1），每线程独立provider，job租约、代次、每次传输前计费、R同事务均保留。门开_parse_job_worker不再套短旧aggregate员工预算（QC依旧独立预算），使用持久五phase账单。

保存job专属md-upload-manifest检查点：{staging_id,manifest}，不按hash扫最新全文/全库。恢复重查该staging的实际document/QC证明，与原检查点比对，变化拒旧游标；不会只因l2_committed就跳过剩余窗口。新增ParseJobPartial，manager记录partial而非强行done，允许明确retry；已提交事实保留。取消检查加入租约检查（含事实检查点、计费、发叶前/提交前），取消后不给后续模型/发布授权，已提交内容不删除；模型不在SQL锁内。UI有“已保留进度/继续处理”，不会将门开partial伪称旧简报重建失败。

### 4. QC结构证据绑定

app/document_quality/gate.py仅门开QC pass时产生evidence_binding={company_id,job_id,file_hash,document_fingerprint}。指纹覆盖完整document（markdown/document_map/assets等），不是status字符串盖章。

Dossier.from_quality严格核对上述scope/源哈希/当前document指纹，缺证明或不等立即拒绝。正在运行仍保留原quality_fingerprint与resume fingerprint检查；没有放宽原件hash、坐标或数字守卫。没有宿主业务判断。旧QC关闭态结果不加新字段。

### 5. 明确有限预算 / 续期

仅只读饭团original-window-audit.json：原件SHA6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965；13937字符/401行/114元素/114窗；无未映射非空行/不可用窗，最大1324字符/33行。未打开/转换真实原件。

门开上传必须明确设置SAM_MD_PHASE_LIMITS为五key JSON，每项整数0..10000；没有默认偷偷放大。示例仅供饭团选定配置，不由CRIS执行：
```
SAM_FACTS_TREE=1
SAM_MD_PHASE_LIMITS={"classify":114,"attribute":114,"extract":228,"plan":114,"leaf":342}
SAM_MD_CORRECTION_ATTEMPTS=2
```
正常114窗五相570请求；示例允许总912请求（提取反馈/叶纠错争用均消耗对应累计上限），不是保证模型能完成或真实验收通过。若旧2次classify/plan配置，会明确partial。账单恢复不清零，deadline仍默认首次30分钟；续跑提高限额必须宿主显式配置。

现有POST /api/parse-jobs/<job>/retry门开接受resume_unresolved bool与renew_deadline_seconds=null或1..1800。UI续跑询问是否明确给新的30分钟期限；不同意不续期。每次retry保存独立request_id，reader记住已应用的纠错恢复/期限请求，进程恢复不会重复套同一续期意图、不会隐式不断续命。直接run的调用方也可传retry_request_id；原U2手动run参数兼容。

### 6. 拒稿有界纠错 / 显式续跑

md_reader每次LeafRejected保留rejected_drafts（原稿+真实错误）于租约保护的任务检查点，不把坏全文再给员工。反馈仅准确错误（截长上限1000）+保留原内容/索引及授权范围的结构要求；新请求重读当前叶，并仅给分配/已读上下文。员工自行修稿，不在后端改数字、分类、证据或公司名称内容。

correction_attempts默认2，可明确0..2；初稿加最多两次纠错，全部经_call先计费，仍受leaf总上限。同叶BaseChanged另受原有竞争上限并废弃过期稿/冲突判断。纠错耗尽留下技术性unresolved及原审计，不洗成成功；独立兄弟叶继续。progress分别记录technical_rejections、pending_todos、unresolved_readings。产品入口有未解决即partial（pending冲突仍正常保留/展示），不以覆盖done替代严格验收。

明确resume_unresolved后重开拒稿/员工未解决分配（不重建成功项，不重提取已提交窗口事实），保留全部原审计与账单。未解决阅读窗口可重新plan，依旧原授予/原指纹；没有凭空增授权。上传的retry request_id只能应用一次；坏来源/数字拒绝不变。

## schema / 接口 / registry影响

唯一新增消费SQL表md_leaf_consumption；控制状态使用既有memory_kv记录job映射快照、显式retry意图和检查点审计，parse_jobs增加运行时partial/reading状态值，不改SQL列/全局schema版本/迁移/既有触发器。默认关闭，无真实数据迁移/清扫，门关不装配新消费表。md待办展示没有新增解决写接口，原md表保留pending。

U3不改技能/catalog/tool-registry；git status中的这些仍是之前U1授权产物，不是本棒变更。provider.py及R/S投影实现、关闭态test_document_read.py/golden不动。

## 新专项（25参数化实例，最终尚未收集复跑）

新增tests/test_facts_tree_consumption.py：
- two_leaf_same_footnote_namespaces_and_notification_ack_failure：同名脚注真出处、消费投影故障原子回滚/不丢通知/成功ACK/零原子卡。
- notification_old_consumer_always_reads_latest_version：旧consumer实例不能消费旧快照覆盖版本2。
- md_todo_scoped_read_and_escaped_non_resolving_card：双方证据/公司隔离/HTML转义/无数字resolve。
- quality_binding_rejects_changed_document_or_scope（7）：markdown/map/assets/hash/company/job/status差异拒绝。
- rejection_two_charged_repairs_then_explicit_resume_keeps_bad_audit：3次拒稿、兄弟成功、准确反馈不含坏稿、显式续跑成功且审计/账单不删。
- upload_seam_partial_resume_and_cancel_no_publish：有限预算partial、无重复前窗、真实provider导览和零旧brief/guide。
- cancel_after_leaf_transport_rolls_back_no_leaf / cancel_during_real_projection_rolls_back_entire_leaf：模型回传后或真实投影中取消，零叶账本/索引/历史/操作/投影，计费与既有事实保留。
- upload_requires_explicit_bounded_totals（3）：缺/非法限额拒绝。
- actual_upload_gate_order_resume_subset_commit_not_full_legacy（2）：实际webapp默认pipeline先转换/QC再五相；事实子集已提交不误done；恢复不重转换；真实provider；映射变更拒绝旧游标，旧全文业务入口设陷阱。
- actual_md_todos_api_host_scope_no_numeric_resolve_and_snapshot：真实API、宿主scope、快照同步、旧int resolve不可用于新pending。
- parse_manager_retains_partial_and_explicit_retry_options：真实manager保留partial、明确retry意图。
- untrusted_cloud_md_view_only_scoped_typed_fields：脏/跨公司快照拒绝及显示白名单。
- explicit_retry_intent_applies_deadline_only_once：同意图恢复不重复续期，新意图才续。
- qc_gate_emits_exact_document_binding：真实隔离QC脚本+离线员工、pass证明精确绑定。
- existing_module_read_consumes_v2_without_model：现有模块read机械v2简报/导览/json。

## 实际执行（严格区分版本）

1. 施工中途相关回归：135 passed / 0 failed，22.86s，8个已知fork多线程DeprecationWarning。命令：tests/test_document_read_parallel.py、test_facts_tree_mapped_md.py、test_document_read.py、test_document_read_leaves.py、test_document_quality.py、test_upload_async.py。此时尚未包含最后实际上传/UI/显式retry意图/模块read修改，不冒称当前终态。
2. 新专项首次：3 passed / 13 errors，0.27s。全为mapped依赖的real_store夹具遗漏导入，首栈`fixture 'real_store' not found`（test_facts_tree_mapped_md.py:32）；仅补fixture import。
3. 修正后专项尝试未收集，exit 4：tests/conftest.py:34 `with socket.socket()`→/usr/lib/python3.14/socket.py:236→`PermissionError: [Errno 1] Operation not permitted`。按纪律不重试、不绕过/修改隔离；之后补全的25例及当前相关回归待饭团正常WSL执行。没有本棒当前全量/真模型/浏览器截图通过数字。饭团负责scripts/dev_browser_shot.sh及最终全量/本地提交/原件验收。
4. 最终compileall、diff-check见下节。本棒没有stage/commit/push/deploy，没有碰harness梦境运行文件，没有复制原trace到外部位置。真实原件/真库未由本棒读取或写入，未做新的SHA验收测量，不冒称已独立复核。

## 限制 / 待饭团验收

目前代码完整交付但最后版本尚未在正常隔离pytest完成回归，尤其真实上传/通知竞争/云展示新增路径需饭团复跑。旧叶证据重写仍拒绝，真实模型能否在两次纠错内选择保留旧块/追加/弃权不能由离线代码宣称。原件114窗/完整真模型覆盖未执行；无自动争议裁决、无新的L1/L0总结模型、无生产迁移。取消保留已提交内容，不承诺撤回已完成事务/网络exactly-once。通知消费用短SQL写锁保证一致性，会与其它短写串行，模型绝不持锁；不把全库检索变成员工阅读授权。

### 最后静态收尾

- `.venv/bin/python -m compileall -q app webapp tests scripts`：exit 0。
- `git diff --check`：exit 0。
- protected diff（provider.py、app/db、test_document_read.py、document_read_closed_b2b1.json、tool-registry）为空。
- 本棒全量未执行；新25实例及当前相关回归因socket阻断尚待饭团正常WSL。无当前通过终态可宣称。
- HEAD仍a1dfe7b；无stage/commit/push/deploy，无真模型/浏览器截图。停在U3离线交付，等待饭团独立回归、审查与完整原件验收。

## 独立审查返工：正式脚本启动及内容待办可读性

饭团前轮三文件78 passed（20.98s，8个既有fork警告）不能证明文件执行入口能启动。原只读探测证实settings导入在路径自举之前，python webapp/app.py会导入自身导致ImportError。本次把唯一该导入移到原_sys.path.insert之后，不改命名/启动脚本、不要求用户设PYTHONPATH。

本次仅收口：webapp/app.py导入位置；_todo_card.html新类型增加md-content-todo独立class；webapp/static/css/app.css仅该类型的min-width/max-width、pre-wrap/overflow-wrap、双栏minmax及窄屏单栏；tests/test_local_startup_orchestration.py新增2参数真实文件入口smoke；test_facts_tree_consumption.py卡片断言补强；本报告。旧数字待办分支/样式不变。

主视觉为旧/新内容和员工理由。leaf_code、内部block位置、资料版本、job、operation/ordinal及全部原件链接/坐标/范围/完整hash/事实ID均保留在“来源和处理记录”details内；不删除证据，不加任何裁决按钮。真实Windows浏览器截图仍由饭团唯一脚本执行，CRIS未截图、不冒称视觉验收通过。

新增真实云只读启动smoke分别SAM_FACTS_TREE=0/1：只给白名单环境、新隔离snapshot/data/HOME、HOST=127.0.0.1/PORT=0，无继承凭证/项目PYTHONPATH/真网关配置；实际执行sys.executable webapp/app.py，观察Flask监听成功后结束进程，并确认未生成本地业务app.db。复用既有标准run.sh入口编排测试，未改conftest或隔离机制。

本轮真实数字（取代此前未复跑终态，不宣称全量）：
- tests/test_local_startup_orchestration.py + test_facts_tree_consumption.py + test_cloud_home.py：**80 passed，15.85s**。
- 补强卡片主视觉/折叠记录断言：**1 passed / 24 deselected，0.52s**。
- test_document_read_parallel.py + test_facts_tree_mapped_md.py + test_facts_tree_consumption.py：**78 passed，18.10s**，0 failed；本轮未出现此前fork警告输出。
- `.venv/bin/python -m compileall -q app webapp tests scripts`：exit 0。
- `git diff --check`：exit 0。

HEAD仍a1dfe7b；未全量长跑、未真模型、未读取凭证、未stage/commit/push/deploy、未碰harness运行脏文件。仅完成本单两项，停手等饭团浏览器/全量/真模型验收。

## 真模型契约歧义返工：已发布旧证据 vs 新增授权

本机只读核对饭团 `/tmp/sam-u2-parallel-real-6cr53880/result.json` 的L-info-01失败assignment：3份rejected_drafts全部为`existing content/index must be retained`，末稿note明确将旧证据换为当前grant；对应response 2865008e2ce048369ce411f6fc9a808e.response.json已定位。没有复制原trace/全文到其它目录。35次请求的严格审计FAIL是饭团结果，不洗成U3通过，不新造成功job。

本次最小改动：
- skills/fact-leaf/SKILL.md：0.5.0→0.5.1。v2明确current.blocks递归逐字逐字段保留（text及标点、block_id、ref、grant_id、file_hash、original_name、source_loc、起止行、fact_ids及其它旧字段）；旧来源/旧ID不必在当前grants/facts中，后端复核已发布证据，不要求重新授予当前员工。仅新增块/conflicts.new_block受本轮授权/事实约束，用不同新block_id/ref。
- app/facts_tree/md_reader.py：compare_submit每次携带结构comparison_contract，区分retained_blocks/new_evidence/no_op；纠错反馈为准确reason+同一字段边界契约。不发送坏稿全文。恢复旧失败job时，若保存了0.5.0的歧义requirement，发送端仅取其reason并装配新契约，不重发旧矛盾措辞；原rejected_drafts/审计/计费不删不改。
- 员工判断只是重复确认时可完整复制current.blocks、conflicts=[]、note，形成no-op操作回执/任务历史；无新增叶版本/内容/索引变更。是否重复仍员工决定，后端没有新增语义去重、自动补回旧块、改文案/证据/标点或替换事实ID。
- 宿主md_publish保留旧块/证据拒绝、数字/原文范围/CAS守卫完全不改；最多2次纠错和计费逻辑不改。
- 官方 `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py` 重生成：9 skills、rejected_skills=0（catalog rejected=[]）、8 tools；既有rejected_tools=1未变。fact-leaf 0.5.1/hash更新，file-processor仍1.2.0，本次无修改；tool-registry零diff。tests/test_phase1_catalog.py同步正式版本。
- 复用跨文件真实provider两进程专项；在既有test_revision_append_replay_and_stale_base补强实际行为：只提供第二grant时，替换旧证据仍拒且零操作写；逐字段保留旧证据可no-op成功、旧正文SHA/修订数量不变。没有为提示文字新增镜像断言，没有新增测试实例。

本轮实际离线结果：
- tests/test_document_read_parallel.py + test_facts_tree_mapped_md.py + test_facts_tree_consumption.py + test_phase1_catalog.py：**82 passed，17.93s**，0 failed。
- 随后补强既有保留证据/no-op行为用例：**1 passed / 38 deselected，0.71s**。
- 最终compileall/diff-check另列；未跑全量/真模型。饭团将对同一个失败job显式恢复，旧审计/账单保留；是否真实模型纠错成功仍待饭团验收，不以离线数字冒称成功。

HEAD仍a1dfe7b；本次不改正式启动/UI/上传/租约/消费/schema/provider，不碰harness运行文件；仅官方catalog为正式技能产物。未stage/commit/push/deploy，不读凭证，不进入其它施工棒。

本次最后静态检查：compileall exit 0；git diff --check exit 0；tool-registry diff为空。完成停手，等待饭团同失败job恢复及最终全量/原件验收。
