# 段 R — 真机投影并入账本事务

## 状态

起点 cc6207a（交接基线 1403 passed）。实现与静态检查完成，未运行 pytest：沿用已知沙箱 conftest socket 受限，保留改动交饭团正常 shell 复跑。不声称测试绿或全量终态数。未 stage/commit/push/部署、未调真模型。

完整读 19 号交接档并核对 provider、leaf_store、rollup、database.connect 和 VectorProjection。发现与详档一致：叶子/冲突件在 store.db 的 IMMEDIATE 中写 provider 另一连接；rollup 当前没有自己的显式写事务。CompanyBindingStore 复用 store.db，不存在本棒未覆盖的第三写连接。

## 改动文件

- app/memory/provider.py：连接补 busy_timeout=30000（仅纵深防御）。新增宿主私有 `_publish_documents_join`：门开、active sqlite connection、规范化同 main 文件、sqlite.Row 检查；在 provider.lock 下构造私有浅拷贝 view，view.db 指向外层连接，不改共享 provider.db。不 BEGIN、不 commit、不 rollback。共享 `_document_bundle` 校验与 `_publish_document_rows` upsert/delete/_project/stale/ready，原普通 publish_documents 的事务/异常分支保留。
- app/facts_tree/leaf_store.py：门开且 isinstance LocalVectorMemoryProvider、规范化绝对路径同文件才走 join；否则保留 publish_pair。叶/冲突已在外层事务内直接 join。投影失败由原 outer rollback 同时撤销账本与投影；join 路径不在 rollback 后另开事务补写 previous（原子回滚已恢复 previous）。非 join 既有补偿不改。
- app/facts_tree/rollup.py：唯一投影写点改经 store 私有 helper；store 在尚无事务时负责 IMMEDIATE + context commit/rollback；若已有外层事务则仅 join，事务归调用方。
- tests/test_facts_tree_transactions.py：新增 14 个参数化实例，真实 LeafPublishService + LocalVectorMemoryProvider + sqlite-vec + 离线 embedding + 临时数据库。
- 本报告。

## 原子性与接缝说明

`_project` 在私有 view 上执行，所有文档与块 SQL 用外层 connection。共享 VectorProjection 在此写路径只调用 blob 的纯向量校验/序列化，不使用其原 connection、不打开第三连接、不重新装配或加载扩展。分块/嵌入/检索逻辑不改。

join 失败抛 ProjectionError，既不跨连接标 stale，也不自行结束事务；外层 rollback 同时恢复 revision/index/links/documents/chunks/projection state。成功时 stale/ready 仍写入同一事务。非 join provider 失败仍按既有逻辑单独标 stale。provider 的公共 publish_documents 签名不变；私有接缝不进工具或员工接口。

## 必加四组测试对应（尚未执行）

1. 真机发叶：test_real_build_leaf_ledger_projection_and_search_same_transaction。真实 builder 发叶、各账本计数为1、投影/向量/read/search；trace 断言唯一 BEGIN IMMEDIATE，修复前同文件第二写连接自锁必挂。
2. 原子回滚：test_join_projection_failure_rolls_back_entire_ledger（leaf/conflict 两实例）；test_failed_update_keeps_previous_revision_and_projection；test_join_failure_leaves_transaction_to_caller。初次失败所有计数0，facts仍在；更新失败保持旧版本字节/state；接缝不擅自 rollback。
3. 真机 rollup/冲突：test_real_conflict_publish_and_rollups；test_rollup_projection_failure_is_atomic。四个 trunk/tree 文档读回、no-op；第二文档投影失败不留下半批。
4. 接缝安全：test_join_preserves_document_validation（uri/company/text 三实例）；test_join_does_not_commit_and_caller_can_rollback；test_join_requires_active_same_database_and_gate；test_join_selection_normalizes_path_and_excludes_other_database。非法结构/跨公司/非文本、未开事务、不同文件、关闭态均受限；join 不执行 BEGIN/COMMIT/ROLLBACK，外层未提交内容对原 provider 读连接不可见。

## 自证与未做项

- `.venv/bin/python -m compileall -q app scripts tests/test_facts_tree_transactions.py`：exit 0。
- `git diff --check`：exit 0。
- `git diff --exit-code -- skills harness-openclaw/skill-registry harness-openclaw/tool-registry tests/test_document_read.py tests/fixtures/document_read_closed_b2b1.json`：exit 0。
- 关闭态条件与旧白名单不改；新 seam require_enabled，store 选择明确门后。假 Memory 走原 publish_pair，原测试未改。关闭态逐字节行为仍须饭团跑 golden 确认。
- harness-openclaw/DREAMS.md 与运行时 dreaming/session-corpus 既有脏文件保持，不 stage；19 号交接档未加入提交。无依赖、技能、catalog 或工具注册改动。
- 未尝试 pytest，未编造新增/全量通过数字；待饭团正常 shell 跑本文件、既有 facts_tree/provider/document_read 与全量后提交。停止本棒。
