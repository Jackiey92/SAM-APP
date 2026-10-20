# 事实树 段S 接单：门开停用旧「自动重建→原子卡复活」链路（含段R断言收口）

## 目标与性质
段R 的 join 同事务实现保留（已验证账本+投影原子回滚正确，1413 passed / 仅 3 failed）。
本棒只做两件事，都是**门开（SAM_FACTS_TREE=True）行为修正，关闭态必须逐字节不变**：

1. 修掉一个真机隐患：门开下旧的「事实变更→投影 stale→检索触发 rebuild_projection→把每条 fact 重新合成原子事实卡 F*.md 灌回投影」链路必须断开。门开记忆层只允许叶子/树枝/树根/冲突件，绝不允许检索动作把原子卡复活。
2. 收口段R 三个失败测试：其断言与既有 stale 语义不符，按本棒门开新语义改正（不是放松原子回滚断言）。

## 根因（已定位，附坐标，勿重复猜测）
- 触发器在 `app/memory/initialize.py:39-47`：`m2_facts_insert/update/delete`，facts 表任一增改删后把该公司 `m1_projection_state` 置 stale。这是既有迁移机关，**本棒不删触发器、不改 schema 迁移**。
- `app/memory/provider.py`：
  - `search()` 461-499：468-472 见 `state is None` 或 `stale+error is None` 就 `self.rebuild_projection(...)`；475-477 二次检查 stale 直接抛 `ProjectionError("company projection stale; rebuild required")`。
  - `rebuild_projection()` 581-603：DELETE chunks / DELETE m1_documents(fact_json) 后，`self._project(self._fact_document(f))` 对**每条 fact 合成原子卡文档重投**（596 行）——这就是原子卡复活点。
  - `query()` 502-518：511-512 `list(rows) + [self._fact_document(f) ...]` 在内存里给目录列举结果拼 F 卡。
- 门开员工检索确实走这条路：`app/memory_map.py:177-196 memory_search → self.memory.search(...)`，由 sam_memory_search 工具（tool_bridge / openclaw_adapter）暴露给员工。所以门开第一次检索就会复活原子卡。
- 段R 失败三例（`tests/test_facts_tree_transactions.py`）：
  - fixture real_store 第 25-27 行插 fact 并 commit → 触发器已把状态置 stale（实测 `before commit store` 即 stale）。
  - `test_join_projection_failure_rolls_back_entire_ledger[leaf/conflict]` 第 75 行断言回滚后 `projection_status(COMPANY) is None`；
  - `test_failed_update_keeps_previous_revision_and_projection` 第 174 行断言 `== 'ready'`。
  - 二者失败仅因 fixture 插 fact 触发的 stale 早已存在，与被测的叶子投影事务无关；账本计数/上一版投影保留/连接状态等原子性断言全部正确，不得放松。

## 硬约束
1. 默认开关 `SAM_FACTS_TREE=False`。关闭态 search/query/rebuild_projection 行为与结果逐字节不变；新增分支一律 `if settings.SAM_FACTS_TREE:` 门内，不重构旧函数外形。
2. 门开不删表、不删/不改触发器、不做新迁移；`m1_projection_state` 表与行在门开下可作为无害遗留存在，但门开任何读/检索路径不得据此触发 rebuild 或报错。
3. 门开零业务词：不新增正则/关键词/枚举/映射/写死公式；模块判定继续只走既有 `_tree_address`/MODULES 单一映射。
4. 门开绝不合成/投影 `_fact_document`（F 卡）：
   - `search` 门开：跳过 468-472 的自动 rebuild、跳过 475-477 的 stale 报错；直接对当前已投影的树文档 chunks 做向量检索（保留 filters 精确值护栏、reranker 身份护栏不变）。无投影时返回空，不得隐式重建。
   - `query` 门开：只返回 m1_documents 中树文档（fact_json IS NULL 的叶子/树枝/树根/冲突件），**不拼接** 511-512 的 `_fact_document` 内存结果。
   - `rebuild_projection` 若在门开被显式调用：只对该公司现存树文档（m1_documents 中 fact_json IS NULL 的行）DELETE+重投 chunks，**禁止**遍历 `_fact_rows`/`_fact_document`；不新建任何 F 卡 m1_documents 行。
5. 段R join 同事务原子性（`_publish_documents_join` / leaf_store `_publish_documents` / rollup）保持不变；失败仍整笔回滚、join 不自开事务不自行 commit/rollback。
6. 门开叶子/树枝发布后，向量检索必须能命中刚投影的叶子 chunks（验证 _project 的 module_key/uri 与 search 的 address/prefix 一致；若门开 search 直接查现状即可命中则无需额外写）。
7. 不 push、不部署、不调真模型。compileall + git diff --check 过即可停手，pytest 由饭团正常 shell 复跑。

## 测试要求
- 修正 `test_facts_tree_transactions.py` 三例断言到门开新语义。建议：fixture 插 fact 后状态为 stale 是既成事实，断言应针对"被测投影事务本身"——失败回滚后不新增脏账本/投影、连接不留在事务、上一版可读；对投影状态的断言改为门开真实承诺（回滚不改变本次事务前的既有状态，且门开 search 不被该 stale 误导）。不得删除原子回滚的计数/上一版保留断言。
- 在真机 provider 测试（`tests/test_facts_tree_provider.py` 风格，真 LocalVectorMemoryProvider + Embedding 桩 + sqlite-vec）新增门开回归，至少覆盖：
  a. 插 facts（状态变 stale）后调 `memory.search`：不抛 stale、不触发 rebuild、返回结果里**无任何 F*/原子卡 uri**，且 m1_documents/m1_chunks 不新增 fact_json 行；
  b. 门开发布一片叶子后 search 能命中该叶 chunk；
  c. 门开 `query(prefix=公司)` 结果不含 `_fact_document` 合成项，只含树文档；
  d. 门开显式 `rebuild_projection` 后仍零原子卡、树文档可检索；
  e. 关闭态对照：stale→search 触发 rebuild、F 卡存在的旧行为不变（若已有测试覆盖则引用，不重复造）。
- 全量由饭团跑，目标只增不减、0 failed。

## 交付物
- 改动：`app/memory/provider.py`（search/query/rebuild_projection 门开分支）；`tests/test_facts_tree_transactions.py`（三例断言）；新增/扩充门开 provider 回归测试。
- 回执：`docs/handoffs/facts-tree-segment-s-report.md`，写清每处门开分支行号、为何不删触发器、新增测试清单、未跑全量（沙箱 socket 限制属预期）。
- 段R 未提交改动在工作区保留，本棒在其上改；是否分段 commit 由饭团复跑后定，CRIS 不要自行 commit。

## 极简现场
- 仓：`/home/jakki/projects/startup-ai-manager`，HEAD=cc6207a + 工作区段R 改动。
- 门开关：`settings.SAM_FACTS_TREE`；门开工具门：`facts_tree.tree_meta.require_enabled`。
- 现有真机 provider 测试参考：`tests/test_facts_tree_provider.py`；事务测试：`tests/test_facts_tree_transactions.py`。
