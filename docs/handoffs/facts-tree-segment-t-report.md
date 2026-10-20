# 段 T — 事实树枝归属交付与叶子写作契约

## 状态与依据

起点 467b40a；派单基线由饭团实测 1423 passed / 1 skipped / 0 failed，本棒未复跑。完整读仓库 AGENTS.md 与 21 号单，并核对 service/verifier/module_tree/leaf_plan/build/store 及现有 fake-worker/provider/catalog 测试。

初次核对发现交接的非法 module_key 拒绝现状与实际代码不同，已停手反馈；按饭团本线程裁决，在 DirectReadService 门开候选校验增显式拒绝，保持共享 validated_module 未知值→None 的现有语义。跨树枝报错按真实路径验证，不改产品报错。

代码与静态检查完成；没有 pytest/真模型运行，没有 stage/commit/push/部署。全量与真机回归由饭团在正常 WSL 执行，本报告不声称验收通过。

## 逐项改动

### §1.1 file-processor

skills/file-processor/SKILL.md：1.0.0→1.1.0。extract 每个 fact 的 JSON 示例增加 module_key:null；明确逐条显式交付合法 code 或 JSON null，员工按事实对象判断、只选一个，不按文件名/出处机械归属，不确定不硬塞。列出静态10枝及隐藏标记，补人员占比/技术归属写法示例及反例。仅 extract 事实项加字段，其余动作契约不改。

### §1.2 门开宿主接线

app/document_read/service.py：仅门开 extract request 注入 module_options=[{key,directory,hidden}]，来自 MODULES 单一数据源。关闭态 payload 不增加字段。门开 _verify：module_key 非 null 时必须是 MODULE_KEYS 的字符串，否则 ValueError；由既有逐候选 try/except 写 unresolved/correction_feedback，不终止整份文件。显式 null/未提供字段兼容，不推断归属。

核对 _verify 以 `{**item, ...}` 传入 verify_candidate，module_key 保留；candidate 写库经过现有 ExtractedFact.module_key 与 consolidate/store 路径。already_delivered 六字段只作去重提示，不影响候选本体，未改。

### §1.3 fact-leaf

skills/fact-leaf/SKILL.md：0.3.0→0.4.0。补每个脚注唯一完整定义、多来源多个脚注、管道表仅允许在数据明细、表格数值/单位/期间逐字对应 fact；给悬空脚注和2600.00→2600漂移反例。不放松后端任何护栏。

### §2 测试与 catalog

新增 tests/test_facts_tree_module_delivery.py：21 个参数化实例，未执行。

- test_open_employee_module_delivery_accept_or_feedback：16实例（10合法枝、null、非法string、整数、bool、list、dict）。验证门开静态清单、合法值完整落库、null落NULL、非法值走候选反馈且零事实写入、文件可完成。
- test_closed_unknown_module_still_unclassified_and_no_options：关闭态未知值仍NULL，不注入选项、不引入拒绝期望变化；原关闭态 golden/测试不修改。
- test_leaf_gets_two_same_trunk_facts_cross_table_rejected：3实例。bizov2条/hr1条，员工只收到2个本枝ID；合法两行通过；跨枝表格ID按实际 `undeclared table fact or row source` 拒绝；跨枝声明ID按 `fact reference outside supplied trunk facts` 拒绝；拒绝分支零revision/index/links与文件写入。
- test_open_extract_to_leaf_real_ledger_projection_atomic：fake模型五相 + 真实provider/LeafPublishService/隔离库。extract归bizov→plan→leaf built1/failed0，账本三表计数1、真实投影可读/chunks存在、无原子卡。复用段R真实事务夹具；R失败回滚测试不动。

更新 tests/test_phase1_catalog.py：保留9技能/rejected=[]断言，版本改为fact-leaf0.4.0并增加file-processor1.1.0断言。
官方 scripts/build_phase1_catalog.py 重生成 harness-openclaw/skill-registry/catalog.json：仅这两个技能版本/content_sha变化；技能仍9，catalog/rejected.json为[]，tool-registry零diff。

## 实际检查

- `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py`：exit0；输出 skills=9 / rejected_skills=0 / tools=8 / rejected_tools=1（工具既有拒绝项，工具产物零diff）。
- `.venv/bin/python -m compileall -q app scripts tests/test_facts_tree_module_delivery.py tests/test_phase1_catalog.py`：exit0。
- `git diff --check`：exit0。
- `git diff --exit-code -- app/module_tree.py app/memory/provider.py app/memory/projection.py app/facts_tree app/db app/memory/initialize.py tests/test_document_read.py tests/fixtures/document_read_closed_b2b1.json harness-openclaw/tool-registry`：exit0。

## 未执行验证与边界

未运行pytest、未取全量终态数字；新增21例/更新catalog断言及既有全量由饭团复跑。未调用真模型，未验证新一轮真实文件built比例/坐标回查/原件SHA，这些属于饭团真机验收。

不改schema/迁移/触发器/R-S投影事务/默认开关/共享taxonomy/数字及出处护栏；不施工Q2，无新依赖。未读取、打印或复制凭证，未读取或修改真实数据/原件。harness-openclaw/DREAMS.md及dreaming/session-corpus等既有运行时脏文件未动、未stage。catalog为本棒授权正规产物，21号交接档未纳入提交。

停止等待饭团回归与提交；本棒无commit hash。

## 同线程返工记录：全坏批次完成语义

饭团正常 WSL 首轮实际全量：`5 failed, 1439 passed, 1 skipped in 178.59s (0:02:58)`。
本次只读核对 `/tmp/sam-segment-t-check-ml3u474_/pytest.log`，五例均为新增测试 wrong/42/False/list/dict 分支对 result['done'] 的 KeyError。全批被拒后既有 service 保持 complete=False，直到预算用尽返回 partial；产品护栏正确，测试错误要求 done。

修正只涉及 tests/test_facts_tree_module_delivery.py 与本回执，产品代码/SKILL/catalog/golden 不再改：

- 合法10枝/null分支继续要求done与正确落库/清单。
- 全非法5分支限定extract预算2，要求partial、complete=False、零accepted/fact_ids/事实；两次逐候选拒绝均校验invalid_candidate/invalid module_key及原始值；第二次extract输入确有上次correction_feedback；只执行classify/attribute/extract/extract，零plan/leaf，零叶账本/投影/记忆产物。移除fake model针对“全坏仍会plan”的错误假设。
- 新增 test_mixed_module_delivery_isolates_invalid_and_publishes_valid_leaf：合法bizov+非法wrong同批，合法兄弟落库、非法候选单独反馈、extract1次即可完成并发叶built1/failed0，三表各1。新增测试总数从21变为22实例（均待饭团复跑）。

修正后 compileall（app/scripts/本棒测试）与 git diff --check 均exit0。按要求未运行pytest，不能声称本轮已经全绿；未commit/push/部署/调真模型，harness运行时文件不动、不stage。饭团复跑后再给最终全量数字与提交hash。

## 饭团 WSL 独立回归（CRIS 交付后）

- 首轮全量：1439 passed / 1 skipped / 5 failed，178.59 秒；五个新增非法候选测试错误要求全坏批次 done。修正测试并补混合批次验证，未改业务护栏。
- 修正后专项：22 passed / 0 failed，2.37 秒。
- 最终全量：1445 passed / 1 skipped / 0 failed，159.17 秒。日志 `/tmp/sam-segment-t-check-ml3u474_/pytest-final.log`。
- catalog 9 技能，rejected=[]，file-processor 1.1.0/fact-leaf 0.4.0 哈希与正文一致；关闭态 golden、段 R/S 事务及投影、tool-registry 均零 diff。
- 本地启动包装已修复退出码透传，仅在忽略的 .runtime 中；不属于业务提交。
- 第六次真机结果另记验收回执，本段不宣称真机通过。

## 第六次真机后手册补强（接续 25cfbc37dbef91f871511898cdda7841ebf41820）

饭团确认前次提交最终全量1445 passed/1 skipped/0 failed；这不是本次执行数字。第六次真机验收未通过：97条事实全部module_key非空，但extract第6次重开已关闭坐标，主链partial、尚未plan/leaf。独立断网质检、零原子卡/检索零复活及真实库原件未变由饭团验证，本次不重复宣称真机通过。

只读核对 `/tmp/sam-b3c-real-zt3_hm0q/result_b3c.json`：stage_failure为extract/employee_transport/employee reopened completed coordinates；remaining_source_locs为88/table、80/table；两条unresolved均因quote不在声明坐标内，quote人为插入 `...` 拼接表格行。
按request文件时间核对第五次700df15fcb3244c0a78ed8c277a4b4da与第六次c219bd7f2c88439aa9d847d799ca4be5：第五次返回[88/table,80/table]；第六次仅授权这两坐标，响应却加回91–129中的已关闭坐标。未修改现场文件。

本次只改手册与其正规版本产物：
- skills/file-processor/SKILL.md：1.1.0→1.1.1。source_sections定义本轮允许集合，map仅导航/校验；remaining只能子集并按原文map顺序，逐项核对集合/重复/顺序；抽象[a,b]正反例，禁止纠错/同义改写/补数重开c，未完成当前坐标必须保留。quote只能连续逐字原文，禁止自行插省略号拼接。
- tests/test_phase1_catalog.py：仅file-processor版本断言改1.1.1，不删测试、不放松期望、不新增镜像文本测试。
- harness-openclaw/skill-registry/catalog.json：官方重生成，仅file-processor版本/content_sha变化。fact-leaf仍0.4.0，9技能，rejected=[]，tool-registry零diff。
- 本报告补记。

实际执行：官方build_phase1_catalog.py exit0（9技能、rejected_skills0）；compileall app/scripts/catalog测试exit0；git diff --check exit0。宿主代码/校验/流程、fact-leaf、schema/迁移/触发器/R-S逻辑及关闭态golden未改；无业务归类规则添加。

本次未pytest、未调真模型、未commit/push/部署，未碰真实库/原件/harness运行时文件；尚待饭团正常WSL回归及第七次真机验证，不宣称验收通过。

### 提取手册补强后的饭团回归

全量 1445 passed / 1 skipped / 0 failed，159.64 秒；compileall 与 diff-check 通过。日志 `/tmp/sam-segment-t-check-ml3u474_/pytest-after-manual.log`。
受保护开发库、既有验收库、原件的行数/哈希/修改时间仍与开工前一致。第六次失败证据保留 `.runtime/acceptance/segment-t/run-6/`；原临时目录已清理。file-processor 1.1.1、fact-leaf 0.4.0；catalog 9 技能且 rejected=[]。第七次真机结果另行记录，不把单测通过等同真机通过。

## 第七次真机后的叶子交付清单补强（HEAD 8790517）

饭团提供本地全量1445 passed/1 skipped/0 failed；本次未复跑。第七次流程done、174条事实、built5/failed14/empty3，所有叶调用facts>0且trunk/tree导览已生成，零原子卡/检索零复活、真库原件未变。但脚本PASS不是严格验收：纳米句仅落mkt叶，bizov原始输出虽有证据句，却因无脚注引导语整片拒绝，21号单要求仍未达标。

只读核对 `/tmp/sam-b3c-real-tb8wmh1e/result_b3c.json` leaves_failed 及重点六片的原始request/response：
- L-bizov-02：纳米技术句带引用，但前面“核心业务板块”引导块无脚注。
- L-info-03：额外###小标题，同条脚注定义用分号串两个坐标/原件名，触发原件名拒绝。
- L-hr-04：年龄/服务年限指标含额外数字区间，表格所有非ID/来源单元格都会按行fact核验。
- L-purch-01：空unit改写为“-”，导致拼接literal偏离。
- L-bizov-15：无适用事实ID却表格fact_id用“-”、fact_ids为空。
- L-mkt-14：正文内管道表且缺适用的数据明细章节。
未修改现场产物，不放松任何护栏。

本次改动仅：skills/fact-leaf/SKILL.md（0.4.0→0.4.1）、官方catalog产物、tests/test_phase1_catalog.py的该版本断言、本报告。手册追加精准交付清单：允许二级章节/每个空行块脚注；定义单原件单坐标且实际使用、多源分编号正反例；表格ID双向一致、来源仅标记、value/unit/period逐字，空unit与unknown保留；非ID/来源单元格数字也须行fact支撑，不添额外序号/区间；仅目标适用事实，不足时有出处叙述或既有empty契约，不伪造。没有固定业务内容灌入规则，没有新增后端格式修复或契约字段。

实际检查：官方build_phase1_catalog.py exit0（9技能、rejected_skills0）；compileall app/scripts/catalog测试exit0；git diff --check exit0；app、file-processor、关闭态golden、tool-registry零diff。fact-leaf0.4.1、catalog rejected=[]，仅该技能版本/content_sha变化。未新增镜像手册文本测试、未删除或放松既有测试。

未pytest/真模型/commit/push/部署，未碰真实库、原件或harness运行时文件。第七次尚未严格通过，本次手册补强也尚待下一轮全量与真机复验，不以脚本PASS替代验收判据。停止。

### fact-leaf 0.4.1 后饭团独立回归

最终全量 1445 passed / 1 skipped / 0 failed，175.27 秒；日志 `/tmp/sam-segment-t-check-ml3u474_/pytest-final-leaf.log`。compileall/diff-check通过。新一轮真机结果另记。
第七次原始证据已保留 `.runtime/acceptance/segment-t/run-7/`，原临时数据已清理；独立审计确认174/174事实有归属、已发布关联跨枝串用0。并非所有叶子输入均非空：无该枝事实的目标仍可收到空列表，详见 independent-audit.json；不能把部分调用的非空观察扩成全部。
本地验收脚本已收紧：纳米技术和提升精度须出现在业务概览叶正文且引用原件坐标77，市场叶命中不足以通过。验收脚本在忽略的 .runtime 下，不改业务护栏。
