# 事实树段R 交接：真机投影自锁（同文件两写事务），并入同一事务

起点 HEAD `cc6207a`（Q1 已合）。全量 1403 passed。Q1 让真模型叶子过了护栏，但第三次真机（SAM_FACTS_TREE=1 真实 provider + 真实 db 文件 + 真模型）在**首片叶子落记忆层**时崩溃。

## 0.1 真机复现（已确认，不要靠猜）
`accept_b3c.py`（仓内 `.runtime/`）跑到 `leaf_store._publish` 第 468 行：
```
sqlite3.OperationalError: database is locked
  at provider.py:370  self.db.execute("BEGIN IMMEDIATE")
  ... provider.py:390 self._stale(...) 也 database is locked
```
根因（两条连接同一 db 文件）：
- `LeafPublishService` 写账本用 `connect(db_path)` 的连接 `self.db`；`_publish` 早已在其上 `BEGIN IMMEDIATE`（两阶段事务，写 `facts_tree_leaf_index`/`leaf_revisions`/`fact_leaf_links`，468 行时尚未 commit，见 leaf_store.py:452-470）。
- 468 行在**该事务未提交时**调 `publish_pair(self.memory,…)`；真机 `LocalVectorMemoryProvider` 用**另一条连接**（provider.py:48，独立 `sqlite3.connect`），`publish_documents` 再 `BEGIN IMMEDIATE`（provider.py:370）。
- 同一线程、同一文件：连接A 持 RESERVED/写锁且要等连接B 的投影结果才 commit，连接B 的 IMMEDIATE 等A 释放 → **自锁死**；这不是短锁竞争，`busy_timeout` 解不开（A 永远不会在等B 期间先提交）。provider 异常处理里写 stale 同样拿不到锁。
- 单测全绿是因为测试注入的是**内存假 memory（dict）**，`publish_documents` 不开 SQLite 事务。

read 不受影响：`read_optional`（leaf_store.py:431）在真机已正常通过（读连接在 IMMEDIATE 下可读）；只有"事务内第二次写"死锁。

## 0.2 必须保持的既有不变量（B1/B2 测试硬约束，不许退化）
1. 账本（leaf_revisions/index/links）与记忆层投影（m1_documents/m1_chunks）**同一原子单元**：投影失败必须整体回滚，revision/index/links 计数为 0、m1_documents/m1_chunks 无该 uri。见 test_facts_tree_leaves.py:88-89、:193、:404 等。
2. 关闭态（SAM_FACTS_TREE=False）逐字节不变；门开才走新接缝；假/内存 memory 的现有测试路径与行为不变。
3. 后端零业务判断；只做存储/事务接缝。

## 1. 目标（只做这个）
让事实树把文档投影写入真机记忆层时，**复用调用方已在 IMMEDIATE 事务中的同一条 SQLite 连接**，不再开第二个写事务；投影失败与账本一起回滚。覆盖**所有**事实树投影写点：叶子发布、冲突件发布、trunk/tree rollup（rollup 经同一 store/publish_pair 路径，有同样自锁风险，需一起验）。

## 2. 实现约束（最小、门后）
- 在真机 `LocalVectorMemoryProvider` 增加一个**宿主内部**接缝，用于"在外部已开事务的连接上执行同一批文档写入"：
  - 形态自定（如 `publish_documents(..., join_connection=None)`，或内部 helper/上下文临时在 `self.lock` 下把本批操作切到传入连接），但必须：
    - 连接入参与既有校验：复用同一套 `_address`/`_tree_address` 地址与白名单校验、单 company scope、metadata company 校验、内容类型校验，一条都不能少；
    - **不得**在该连接上再发 `BEGIN IMMEDIATE`，**不得** commit/rollback（事务边界归外层 leaf_store）；
    - 仍在该连接上完成 m1_documents upsert/delete、`_project` 生成 m1_chunks、stale/ready 状态写入；`_project`/VectorProjection 要能在传入连接上执行（最小参数化，不改变分块/嵌入逻辑）。
- leaf_store 仅在"`self.memory` 是真机 provider 且其 db 文件与 `self.db` 同一文件"时走 join 接缝；否则（测试假 memory、其它 provider）保持现有 `publish_pair`。判定同文件用规范化绝对路径，禁止按类型名字符串猜。
- 失败补偿路径（leaf_store.py:472-475 except 里回投 previous）在 join 模式下也必须落在同一外层事务上，随 rollback 生效，不另开事务。
- rollup 的 trunk/tree 写入同样走该接缝（它若也在 store 事务内）。
- 给 provider.py:48 的连接补 `PRAGMA busy_timeout=30000`（与 db.database.connect 对齐，纵深防御），但明确：它不是本缺陷的修复，真正修复是单连接并入事务；不要据此跳过接缝。
- 该接缝是内部宿主存储接口，不进工具注册表/技能/catalog；不得变成给员工的新工具。

## 3. 必加测试（真机存储级，离线，禁用网络/socket 依赖）
新增/扩展用真机 provider 的集成测试（embedding 仿 `tests/test_facts_tree_provider.py` 的 `Embedding` 桩 + `tests.memory_setup.migrated_memory`）：
1. **真实 provider + 真实 LeafPublishService/build_leaf 发布一片合法叶子成功**：revision/index/links 各 1，m1_documents/m1_chunks 有该叶，read 回读一致，search 命中；这是本棒主回归（直接复现自锁，修复前必挂、修复后必过）。
2. **投影失败原子回滚**：让该批在 join 连接上的投影抛错（如 monkeypatch VectorProjection._project/分块抛异常），断言 LeafRejected/ProjectionError 后 leaf_revisions/index/links 计数为 0，m1_documents/m1_chunks 无该 uri（同事务回滚，非补偿残留）。
3. **rollup 真机**：发布若干叶后 rollup_trunk/rollup_tree 在真机 provider 上成功写出 trunk-brief/trunk-guide/tree-brief/tree-guide 且可回读，无锁错误。
4. **接缝安全**：join 路径仍拒非法 uri/跨 company/非文本；关闭态走老路径、golden 不变；假 memory 路径行为不变（老测试不改也全绿）。
全量只增不减、0 failed。

## 4. 边界
- 只本地 commit，不 push/部署；不改业务规则、不调真模型；harness 既有无关脏文件（DREAMS.md、memory/dreaming 等）不要 stage。
- 沙箱若 socket 受限跑不了 pytest：保留改动并在回执说明，全量由饭团在正常 shell 复跑提交；不要因此放宽任何校验。
- 遇与本档冲突或发现还有第三个连接/事务边界，停手列点，不自行发明方案。
