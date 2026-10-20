# 段 P — 未完成验收，沙箱阻塞

## 状态

起点 a891ed3277d27ee3818a79c9ba9550779af43b09；当前未提交、未暂存，无本棒 commit。未 push/部署/调真模型。停止等待可正常运行测试的环境，不进入下一棒。

完整读取 17 号交接档并复核 provider、paths、MODULES、leaf_store/rollup 通用调用契约。离线真实 provider 可由 tests.memory_setup.migrated_memory + synthetic embedding 构造；初轮已成功构造。产品接线与测试草稿已落盘，但不得视为通过验收。

## 改动文件

- app/memory/provider.py：新增内部 `_tree_address`；只在 SAM_FACTS_TREE 门后识别叶子、trunk/tree 文档与冲突件，中文目录映射来自 MODULES，反算 canonical URI。读删沿用 `_address`；扩展 publish_documents 白名单；原事务/投影/upsert/回滚代码不动。未接 publish_leaves，未改内核调用。
- tests/test_facts_tree_provider.py：真实 LocalVectorMemoryProvider、临时数据库、离线 embedding；计划新增 23 个参数化测试实例。
- 本报告。

## §3 测试对应

1. test_leaf_bytes_english_module_null_fact_and_vectors：字节、英文 module、NULL fact_json、chunks、真实向量 search。
2. test_trunk_and_tree_rollups_roundtrip：四类 rollup。
3. test_conflict_roundtrip：冲突件。
4. test_repeat_upsert_has_no_duplicate_documents_or_chunks：重复 upsert。
5. test_delete_and_publish_pair_compensation（2 实例）、test_failed_batch_rolls_back_bytes_and_vectors_marks_stale：删除及批次失败回滚。
6. test_invalid_tree_uri_rejected_without_writes（14 实例）、test_cross_company_metadata_and_uri_rejected：安全拒绝。
7. test_closed_tree_rejection_and_legacy_bundle_unchanged：关闭态异常与旧四类 brief/guide。
8. 既有 document_read/facts_tree 与全量回归：因下述阻塞尚未完成。

## 实际执行结果与完整阻塞

初轮：23 failed in 0.43s。全部是新测试误将 publish_pair 的 keyword-only metadata 传为位置参数：

```
TypeError: publish_pair() takes 2 positional arguments but 3 were given
```

已将调用改为 metadata=，未改产品契约。修正后一次重跑在收集阶段失败（exit 4）：

```
ImportError while loading conftest '/home/jakki/projects/startup-ai-manager/tests/conftest.py'.
tests/conftest.py:34: in <module>
    with socket.socket() as listener:
/usr/lib/python3.14/socket.py:236: in __init__
    _socket.socket.__init__(self, family, type, proto, fileno)
E   PermissionError: [Errno 1] Operation not permitted
```

未重试 socket、未绕过或修改 conftest 隔离护栏。全量未运行，不能声称 0 failed/0 error 或新增测试已绿。用户要求的最终 pytest 收尾数字尚未取得。

`.venv/bin/python -m compileall -q app scripts`：exit 0。
`git diff --check`：exit 0。

关闭态 golden、tests/test_document_read.py、app/facts_tree/、skills/ 等受保护文件 git diff --exit-code 为 0；关闭态原分支代码保留，但行为测试仍待通过。

## 工作树边界

本棒仅修改上述 provider、新测试与报告。harness-openclaw/DREAMS.md 的既有修改与 harness 运行时未跟踪文件保持原状，没有 stage。13–17 号交接档为原有未跟踪文件，未 stage。catalog/tool-registry/SKILL 无本棒改动。

## 待做

在允许 conftest socket 的正常环境重跑新增测试，修复可能剩余测试问题、补齐旧事实卡门开/关闭回归证据，再全量 pytest、复核 diff、提交本地 commit。当前报告不是完成交付。
