# 段U原件返工：连续授权 / 空叶回执 / 数字定位

起点7c818b08b348b1316dca480ea7efbd0c4ea2f8cd。饭团基线1534 passed/1 skipped不是本轮全量/原件通过。只读原临时目录result.json核对：同u-original job仍partial，cursor=17，累计calls 18/19/19/18/15，共89；已有成果及失败审计保持，CRIS未操作真实临时库、未读取原件/凭证、未真模型或复制原trace。

## 最小修复

1. **同一行集授权边界**：新增app/facts_tree/grants.py::range_is_granted，先检整数（bool拒）、顺序、跨度不超过实际lines数量，再逐行检查授权键。md_reader分配、md_publish证据范围与定位fact范围统一复用，不要求落在单个ranges段。因此相邻表头46..47与数据48..52合并46..52合法；46..47和80..85仍不能合并穿越未读缺口。无补读、剪裁、坐标改写或语义规则。
   selected.complete_coordinate按实际选择行与Dossier同坐标全部行机械重算；活跃旧检查点/旧draft恢复也重算，不能只修新allocation而让原失败job继承旧True。模糊旧fact不享受子范围的完整坐标权限；定位fact保持精确行范围/指纹核验。
2. **叶子no_content**：md_reader新增目标匹配、非空reason（≤2000）的outcome=no_content分支；blocks/conflicts/assignments若存在只能空列表。不要求base_version/sha，因为没有内容更新。原draft、理由、责任job、确定性operation_id、授予指纹/ID在租约保护的assignment检查点保存，status=no_content并列入leaves_empty。无新叶、修订、正文/索引变更或事实；现有叶保持原样。
   这类处理回执存在任务KV中，不假装一次叶发布、不在md_leaf_operations/leaf_revisions造空写历史。恢复/已完成重放跳过该terminal分配；checkpoint中断重构reader不会重复leaf调用/回执，旧generation不能保存。普通写稿原base/CAS/保留旧块/出处守卫不动；旧失败rejected_drafts保留，明确resume_unresolved才重交。是否没有该叶内容由员工判断，不由后端标题/关键词判定。
3. **数字错误定位**：leaf_store.check_union只在原拒绝位置追加实际parser literal；facts/quote匹配表达式和判据一字未改。md_publish补block_id（长度截限）、paragraph/row或header-row定位，仅指出本次被拒稿的失败片段，不回传其它块/未读数字，也不替换、计算或格式化值。合成日期2014-09-09原格式通过；在已获授权年份凭证下改写为中文日期，实际失败literal='9月'，依旧拒且零写入。
4. **契约/目录**：fact-leaf 0.5.2→0.5.3，声明叶级no_content无写入格式，有真实内容但结构困难不能借此逃避守卫；日期格式也须原文复制，YYYY-MM-DD不能改中文年月日。compare_submit结构契约同步。file-processor保持1.2.1不改。
   官方build_phase1_catalog.py：9 skills/rejected=[]，8 tools/既有rejected_tools=1；正式版本断言更新，tool-registry零diff。

改动：app/facts_tree/grants.py、md_reader.py、md_publish.py、leaf_store.py（仅报错）、app/document_read/service.py（门开leaves_empty回执归集）、skills/fact-leaf/SKILL.md、官方catalog、test_phase1_catalog.py、新tests/test_facts_tree_grant_receipts.py及本报告。无schema/迁移、无provider/R/S改动，关闭态原service分支/golden不变，未碰harness运行文件。

## 实际专项 / 静态检查

新增22实例，真实LocalVectorMemoryProvider、隔离SQLite、离线替身员工：
- default_adjacent_header_data_allocation_and_evidence_span_write：连续表头+数据跨段选行、证据引用实际发叶。
- bad_evidence_gap_huge_wrong_type_zero_side_effects / bad_allocation_gap_huge_wrong_type_never_reaches_leaf（各5）：真缺口、恶意大跨度、bool/字符串、倒序拒绝，零叶/修订/投影/操作/待办。
- selected_subset_cannot_inherit_complete_coordinate_or_legacy_fact：先预算partial，再模拟原旧检查点错误True，重构后标志False、模糊旧fact不泄漏、精确定位fact仍可用。
- leaf_no_content_keeps_original_or_no_leaf_and_replays_once（2）：空仓无假叶/已有叶不变，回执/原稿/任务可追溯，重放无重复。
- invalid_empty_receipt_rejected_then_explicit_resume_retains_audit（5）：空理由/错型/错目标/非空blocks/非法assignments拒绝，明确同job续跑保留原审计及账单。
- date_original_passes_rewrite_reports_actual_literal_block_and_row：日期原格式通过、改写仍拒且literal/block/row准确。
- leaf_no_content_checkpoint_failure_reconstructs_without_repeating_leaf：回执保存中断续跑幂等。
- old_generation_cannot_save_leaf_no_content_receipt：旧代拒写，已计费调用保留。

首轮新20例+catalog4例：24 passed，2.91s。补齐2个恢复围栏案例后，八文件相关回归216 passed，29.31s；随后补强活跃旧grant重算，新22例+catalog4例26 passed，3.07s。最终相关回归收尾另列，不冒称全量或真模型通过。

## 原库同job恢复边界

原窗口17的准备阶段已成功，allocation失败未推进；普通恢复仅再plan/leaf，不重做其classification/已提交提取。早先空叶/日期拒稿需饭团在同一job明确resume_unresolved重交，保留旧89请求和全部已有成果/审计。过期deadline仍须明确续期，调用账单不重置。

理想114窗的114次phase上限不含已经失败的调用：旧plan失败也已计费，修后覆盖可能需要至少115次累计plan；已有attribute纠错亦需余量。请饭团明确总预算，不由代码静默放大。原件仍FAIL，等待同库续跑与全量验收。CRIS未全量长跑、未真模型、未stage/commit/push/deploy。

最终当前版本八文件相关专项：**216 passed / 0 failed，26.50s**。compileall exit 0；git diff --check exit 0；protected diff（app/db、provider、原关闭态测试/golden、tool-registry）空。HEAD仍7c818b08b348b1316dca480ea7efbd0c4ea2f8cd，未提交。完成停手，等饭团原同job续跑、全量和本地追加提交。
