# 事实树 B3b 回执：门开记忆层切换

起点 HEAD：`8aeccd2d556e1e516d52a86343c950fee830f3c5`，基线 1341 passed / 1 skipped。
仅本地交付，未 push、未部署；停手等饭团真机黑盒，不进入 B3c 真机真模型。

## 0 / 0.1 代码核对

完整读 16 唯一施工口径并核对真实接线及两组夹具。run 的早退只看 state.get('done')；最终 done 设置在叶子阶段成功收尾后，不依赖 brief_done/guide_done。
SQLite facts 的 consolidate_candidates 提交仍在投影/叶子阶段之前；ConsolidationService.list_facts 直接读 SQLite，不依赖原子卡。

## 改动文件与产品接线

1. `app/document_read/service.py`：唯一产品改动，两个关闭态条件。
   - 旧 L2ProjectionService.publish 加 `if not settings.SAM_FACTS_TREE`。
   - 旧 L1/L0 重建条件改为 `if not settings.SAM_FACTS_TREE and state['fact_ids']`。
   - 门开的 _run_leaves、SQLite 提交、runtime KV、预算和断点不变。仍使用 classify/attribute/extract/plan/leaf 五相，无新增或重命名阶段；门开 fresh state 的 brief_done/guide_done 仍为 False，不虚报旧件已建，不新增 legacy_skipped 等兼容键。
2. `tests/test_document_read_leaves.py`：迁移既有门开预期，保留全部用例数与旧护栏断言；关闭态 golden 用例函数逐字节不变。
3. `tests/test_document_read_tree_memory.py`：新增 4 项树-only/断点/历史保留测试。
4. 本回执。

未改 l2_projection/l1_brief/l0_guide/provider/schema/consolidation、任何技能/prompt、预算映射、事实树发布/rollup、默认开关、前端或依赖。未删表、未迁移或清扫历史库；harness 既有无关脏文件与开工前未跟踪派单原文不 stage。

## 迁移的既有门开测试

11 个测试函数涉及预期迁移（含参数化共 14 个 pytest 用例）；其余门开预算输入、scope/fingerprint 等断言照旧，原 25 个 document_read_leaves 用例数不减：

- test_open_complete_same_channel_and_order：actions 不再带 brief/guide，extract 实际调用数由 3 变 1；同源、落盘、汇总、幂等断言保留。
- test_leaf_budget_checkpoint_resume_without_duplicate：恢复 actions 移去旧摘要尾部；plan 只一次、target/revision/计费断言保留。
- test_zero_leaf_stage_budget_then_resume（plan/leaf 两项）：零预算 partial 行为不变；恢复不再补旧摘要。
- test_bad_leaf_is_terminal_and_does_not_block_other_trunk、test_failed_terminal_not_retried_after_budget_partial：仅移去旧摘要，坏叶隔离/terminal skip 保留。
- test_transport_charge_partial_keeps_facts_and_first_leaf：仅移去旧摘要，失败预扣/保留 facts/首叶/精确重试断言保留。
- test_gate_open_resume_old_partial_state_adds_new_call_phases：门开恢复不补旧摘要；先前关闭态 partial 的历史原子卡不主动清理。
- 原 test_leaves_done_resume_only_brief_guide → test_leaves_done_resume_is_noop_without_legacy_summaries：extract=1 下叶子完成就 done；False 的旧摘要标记不影响完成，恢复不增加 actions/revision。
- test_empty_and_invalid_plan_checkpoint_without_leaf_calls（两项）、test_plan_transport_failure_charged_and_recoverable（两项）：恢复动作去掉旧摘要，其余诊断/预扣/恢复检查保留。
- 原 test_rollup_failure_retries_only_rollup_then_summaries → test_rollup_failure_retries_only_rollup_without_legacy_summaries：只恢复机械汇总，不新增摘要动作；partial 与 revision/plan 不重建断言保留。

LeafModel 原“plan 前已有原子卡”过渡断言改成查询 SQLite 已有核验事实，继续验证 facts 已提交再进入叶子阶段，不再要求本棒明确禁止的原子卡投影。

**不改** tests/test_document_read.py、tests/fixtures/document_read_closed_b2b1.json；test_closed_state_and_old_outputs_no_leaf_stage 的源码与 HEAD 逐字节相同，其 state JSON 与全部 5 份文档 golden 比对原样运行。

## 新增 4 项测试与门开证据

1. test_open_skips_all_legacy_services_and_provider_rebuild：把旧 L2 publish / L1 rebuild / L0 rebuild / memory.rebuild_projection 改成失败哨兵，门开仍 done。记录 memory.put 并拒绝 layer=2b_L2；实际写入 URI 集合恰为一片叶、trunk-brief/trunk-guide、tree-brief/tree-guide，5 个内容均非空。Model actions 恰为 classify/attribute/extract/plan/leaf；extract=1、brief_done=guide_done=False；SQLite list_facts 可读 verified 值且 fact_leaf_links 回链该 id。
2. test_partial_and_resume_never_write_legacy_outputs：leaf 上限 1 在双叶途中 partial，facts 在、首叶在；提高预算后只补第二叶，plan 不重复、首 revision 不变、extract 仍 1；partial 和 done 两次都检查无 2b_L2/F* 原子卡 URI、无旧 root brief/guide、无旧摘要 actions。
3. test_all_bad_leaves_keep_sqlite_facts_without_legacy_fallback：两叶坐标不合法均终止拒收，仍按已有收尾语义 leaves_done/done；SQLite verified facts 保留，记忆层零文档，不用旧摘要或原子卡做兜底。
4. test_gate_open_does_not_clean_existing_legacy_documents：预置历史 F99/旧 brief/旧 guide 文档，门开处理后原字节原样保留；只新增事实树，不清历史、不刷新旧件，旧摘要标记仍 False。

所有模型为注入假 Model，所有数据为 tmp_path 临时 SQLite/文档端口；无真实 app.db、无真模型、无新依赖。

## 实测结果

```text
.venv/bin/python -m pytest -q tests/test_document_read.py tests/test_document_read_leaves.py tests/test_document_read_tree_memory.py
58 passed in 5.91s
```

= 原关闭态 29 + 原 leaves 25（有意迁移）+ 新增 4；0 skipped / 0 failed / 0 errors。
首轮曾 54 passed / 3 setup errors：新文件漏导入 open_task 依赖的 task fixture（fixture 'task' not found）；补齐导入后 57 passed，补历史保留用例后重新跑为上述 58 passed。没有隐藏失败、删减原用例或改关闭态 golden。

- 全量 `.venv/bin/python -m pytest -q`：`1345 passed, 1 skipped in 143.68s (0:02:23)`，0 failed / 0 errors；1341 基线 + 新增 4，原测试数不減。
- `.venv/bin/python -m compileall -q app scripts`：退出 0。
- `git diff --check` 与 `git diff --cached --check`：均退出 0。
- 禁改文件/目录及 catalog/tool-registry 零 diff；新增产品 diff 仅 service.py 两处接线条件，未加业务词/正则/指标枚举/第三方 import。
- 默认关闭态旧原子卡与 brief/guide 行为由原 29 项 + 字节 golden 用例验证，旧 state 形状和预算仍保持。

## 未做项

未清扫存量原子卡/旧摘要、未改 provider 检索面或 schema、未做 B3c 鸿通晟真机端到端、真实模型、push/部署；未扩展其它 ingestion 调用方，本棒只改指定 document_read 接线。报告不替代饭团真机黑盒与放行，完成即停手等验收。
