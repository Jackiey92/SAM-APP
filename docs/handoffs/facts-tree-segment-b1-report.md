# 事实树段 B1 完工报告

## 范围与依据

- 依据：`docs/architecture/11-facts-tree-handoff-B1.md`，包含三次裁决；施工基点 `4199524`。
- 只完成 B1：叶子发布/结构与出处护栏、append-only revision、独立归属映射、机械 trunk/tree 汇总。
- 默认关闭、未接主流程、未 push、未部署；不进入 B2/B3。本报告是代码自验，不替代饭团真机黑盒、Gate 或范范放行。
- 原“facts 新增 leaf_code”方案已经撤销：facts DDL 完全不变，未加列、未 ALTER、未对旧读取点加过滤补丁。

## 文件清单

| 文件 | 内容 |
|---|---|
| `app/db/schema.py` | 新增隔离 `fact_leaf_links`、`leaf_revisions`、稳定 URI 索引 `facts_tree_leaf_index` 及索引；facts DDL 不变 |
| `app/memory/initialize.py` | 仅补独立新表 IF NOT EXISTS 初始化；没有 facts ALTER，既有初始化逻辑不变 |
| `app/memory/paths.py` | 仅新增 conflict_uri、tree_brief_uri、tree_guide_uri；现有 helper 签名不变 |
| `app/facts_tree/leaf_store.py` | LeafPublishService、LeafRejected、QuoteResolver/Null/InMemory、结构/引用/数字/历史护栏与两阶段发布 |
| `app/facts_tree/rollup.py` | FactsTreeRollup 及 rollup_trunk/rollup_tree；机械搬运、active/非空过滤、数字追溯、锁与幂等 |
| `tests/test_facts_tree_leaves.py` | 叶子/映射/事务/关闭态快照验收 |
| `tests/test_facts_tree_rollup.py` | 冲突裁决、机械汇总、空树、归档过滤、数字护栏、幂等与回滚验收 |
| `docs/handoffs/facts-tree-segment-b1-report.md` | 本报告 |

零新增第三方依赖。未修改 settings、模板包、绑定服务、provider、原子卡或模块投影文件。既有 `harness-openclaw/` 改动未触碰，不纳入本棒。

## 实现要点

- 复用 `settings.SAM_FACTS_TREE` / `tree_meta.require_enabled()`，没有第二开关或新 facts_tree_enabled 函数；关闭态构造发布服务也会拒绝，不创建库文件。
- 普通叶使用绑定包 `LeafCode.trunk/shape`：s 必含数据明细，n 可省，正文数字仍必须通过 facts value/unit 或本段脚注 quote 校验。
- 原件 sha 由 manifest 验存在并校验公司归属；脚注全量引用/定义匹配，拒绝重复、悬空、未声明出处；不判断坐标文本业务语义，也不连接真实净稿存储。
- 数据明细按行 fact_id 检查公司、verified/未失效/未替代、module_key 和声明集合；数字只在该行原子事实核验。归属另读独立 links 与事实 join，不污染任何旧 facts 字典。
- 编辑历史按空白归一化后的行集合保留旧行并新增当前版本一行；服务对 revisions 只 INSERT，无 UPDATE/DELETE 接口。上版投影 sha 必须与 revision 一致。
- 同一 SQLite 事务写 revision、首次 URI 索引及普通叶 fact_leaf_links UPSERT；publish_pair 成功后才 commit。文档发布异常、映射写异常、commit 异常均覆盖回滚/恢复测试。
- 内容 sha 相同则不重写、不新增 revision、不重复插映射；仍复验引用事实/出处，防止撤销后的事实绕过护栏。映射允许同一事实挂同 trunk 多叶；不实现 B2 的重归属/删除旧关联。
- 冲突件使用独立 conflict code、系统枝 `待办与冲突` 和 target_trunk/target_leaf/metric，禁止下划线，只收 conflict。不写目标叶归属映射，不进 active 汇总，不实现冲突归档动作。
- URI 索引固定首次文件名，模板 title 后续变化不漂移。独立冲突码撞同一个文件地址会拒绝，不能覆盖其他冲突件。
- 汇总只机械拷贝原句/元信息；脚注编号加叶子前缀避免碰撞，原件与坐标不改；汇总数字使用既有 numeric_guard 验可追溯。空树不产文件，最后 active 叶归档后清除过期汇总。整树拒绝过期/伪造 trunk 字节，防止搬入无源结论。
- 文档端口显式注入，沿用既有 read/publish_pair 协议；本段单测用临时文件端口。未接生产 provider、未改其白名单或检索/vector 投影，未安装真实 QuoteResolver。

## 护栏与测试对应

| 验收点 | 测试 |
|---|---|
| facts 零变更、新老库建映射表、两个索引 | `test_new_and_old_schema_facts_unchanged_and_links_created` |
| 迁移前后关闭态原子卡/module fact_sources 逐字节一致 | `test_disabled_migration_preserves_atomic_card_and_module_fact_sources_bytes` |
| 版本自增、历史旧行不变、唯一约束、幂等 | `test_versions_append_only_unique_and_noop` |
| 映射 UPSERT、no-op 不写、同事实多叶、唯一约束 | `test_mapping_upsert_noop_and_one_fact_can_link_multiple_leaves` |
| 跨公司/跨 trunk/不在声明集合/失效/替代/不合法映射 | `test_invalid_artifacts_no_files_no_revisions` 参数项；`test_corrupt_mapping_rejected_without_new_projection` |
| 未知码、缺绑定、frontmatter/YAML、s 必备段、乱序/重复/缺口标题、悬空/重复脚注、伪造 sha、无源数字 | `test_invalid_artifacts_no_files_no_revisions` 参数项；拒绝后检查无文件、无 revision/URI 索引和无新增映射 |
| n 可省数据段、facts 数字并集 | `test_narrative_without_data_and_fact_union` |
| Null/quote 命中与不命中、禁止借别段 quote | `test_quote_and_null_resolver_paths`；非法产物参数 `quote-other-paragraph` |
| 历史改写、重复/额外版本、投影被改写、空白归一化 | `test_history_rewrite_rejected_preserves_previous`、`test_history_integrity_rejected`、`test_history_whitespace_normalization_preserves_rows` |
| 发布失败/commit 失败/映射失败/更新失败恢复 | `test_publish_failure_rolls_back_first_and_update`、`test_commit_failure_restores_document_and_revision`、`test_mapping_insert_failure_rolls_back_revision_and_index`、`test_failed_update_restores_existing_mapping_job` |
| 同叶 URI 稳定、模板改名不漂、并发重放 | `test_versions_append_only_unique_and_noop`、`test_stable_uri_survives_template_title_change`、`test_concurrent_duplicate_is_idempotent` |
| 默认 False、关闭态发布拒绝、旧白名单/投影不变 | `test_default_process_off_and_disabled_constructor_does_not_create_db`、`test_off_gate_no_side_effects`、`test_disabled_existing_provider_whitelist_and_projection_bytes` |
| Markdown 代码块不能伪造段标题、CRLF 原字节保留、期间护栏 | `test_fenced_heading_is_not_a_required_section`、`test_crlf_markdown_keeps_published_bytes`、`test_period_tokens_after_prose_space_are_guarded` |
| 冲突独立码/目标字段/无下划线/只收 conflict/独立 URI/不入汇总 | `test_conflict_independent_iterable_and_excluded`、`test_conflict_ruling_rejects_invalid_metadata`、`test_conflict_guards_no_half_product` |
| 冲突表引用与数字护栏、地址碰撞不能覆盖 | `test_conflict_table_references_and_sources`、`test_conflict_address_collision_never_overwrites_previous` |
| active 非空才汇总、归档/冲突过滤、空树无文件、纯数据叶可见 | `test_archived_and_empty_leaves_excluded_and_stale_removed`、`test_empty_company_and_trunk_no_files`、`test_data_only_active_leaf_is_not_empty` |
| 汇总数字无源拒绝、陈旧 trunk 拒绝、幂等、加锁/失败恢复 | `test_rollup_numeric_rejection`、`test_tree_rejects_stale_trunk_prose_without_numbers`、`test_trunk_tree_copy_and_noop`、`test_rollup_pair_failure_restores_both` |
| 多叶脚注坐标保留、多 trunk 顺序/非空过滤 | `test_multiple_leaf_footnotes_preserve_coordinates_and_are_namespaced`、`test_multiple_trunks_order_only_nonempty` |

## 真实自验

- 修复材料环境后既有基线：1113 passed / 1 skipped，123.83s；之前 /tmp/sam-m5 缺失造成的两项失败不作为当前通过证据。
- 当前新增用例：105 passed，7.96s。
- 最终全量：**1218 passed / 1 skipped，136.03s（0:02:16）**；既有 1113 + 新增 105，零失败。命令：`.venv/bin/python -m pytest -q -p no:cacheprovider`。该数字来自独立映射表最终方案，不沿用作废列方案的结果。
- `.venv/bin/python -m compileall -q app scripts`：通过。
- `git diff --check`：通过；新未跟踪源码亦单独做 no-index 空白检查。
- 所有新增测试只用 pytest 临时库/临时目录，acc-* 公司；未修改真实公司库或真机材料。

## 隔离自证（最终复核命令）

```sh
.venv/bin/python -c 'from app import settings; print(settings.SAM_FACTS_TREE)'
# False

git grep -n -E 'LeafPublishService|publish_leaf\(|publish_conflict\(|rollup_trunk\(|rollup_tree\(|facts_tree\.(leaf_store|rollup)' -- app scripts ':!app/facts_tree/*'
# 无匹配：新入口未被 document_read/ingest/webapp/scripts 等旧主流程引用

git diff -- app/settings.py app/memory/provider.py app/memory/company_facts/l2_consolidation.py app/memory/company_facts/l2_projection.py app/memory/company_facts/module_projection.py app/memory/company_facts/l1_brief.py
# 无输出：不开旧路径过滤补丁、不改原子卡/模块/provider 行为

git grep -n 'fact_leaf_links' -- app scripts ':!app/facts_tree/*' ':!app/db/schema.py'
# 无匹配；initialize_memory 仅引用独立建表常量，不读写映射行
```

关闭态字节一致不是只凭 grep：迁移前后真实临时库原子卡、完整模块 brief JSON、fact_sources 序列化快照测试均通过；facts 列名、列顺序、旧行也保持完全相同。

## 本地提交与停手

- 实现提交成功：`0cd8e56bda664d7c748daca0bd55049e5d664206`（短 hash `0cd8e56`）。`git add` 与约定 message 的 `git commit` 均成功；没有触发 index.lock 只读错误，也未绕过沙箱。该提交仅包含本报告列出的 8 个文件，暂存区为空。此处提交结果补录作为独立本地文档提交；实现 hash 指向已完成全量验证的代码。
- 仅提交本报告列出的 8 个文件；不纳入 harness-openclaw、__pycache__ 或运行时文件。
- 实现与回执均仅本地提交，停手等饭团黑盒；未 push、未部署、未做 B2/B3。没有残留本棒运行时文件，既有 harness-openclaw 工作区状态保留。
