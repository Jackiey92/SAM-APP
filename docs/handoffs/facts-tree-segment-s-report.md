# 段 S — 门开断开 stale 自动复活原子卡链路

## 状态

完整读取 20 号接单档；在 HEAD cc6207a + 段 R 未提交工作区上实现。R join 接缝、叶子/冲突/rollup 事务归属与原子回滚实现不再改动。未跑 pytest（按派单由饭团正常 shell 复跑），未 stage/commit/push/部署、未调真模型。

## 本棒文件与分支行号

- app/memory/provider.py：461 `_tree_rows` 复用 `_tree_address` 与 MODULES 的 canonical 判断，从 fact_json IS NULL 的持久化行中只选真正树文档；忽略历史旧 brief/guide/F 卡与非法形态，不做清理。
- 同文件 480–490：门开 search 不查 stale 决定 rebuild，不因 stale 抛错；关闭态原自动 rebuild 与 stale 错误逻辑保留。
- 同文件 500–506：门开把当前已持久化树文档 URI 清单送入向量候选筛选；原 filters 精确值校验和 reranker 身份校验保持。缺投影返回空，不合成卡、不隐式写。
- 同文件 529–535：门开 query 只用持久化树行；关闭态继续拼 `_fact_document`。
- 同文件 605–619：门开显式 rebuild 用 IMMEDIATE 原子重投现存 canonical 树文档，_project 仅 DELETE/重建每个树 URI 的 chunks；不遍历 facts、不合成卡、不删历史文档/旧块，不修改遗留 stale 行。旧 rebuild 分支保留。
- app/memory/projection.py：38/46–49，为上述门开调用增加可选 document_uris 候选白名单（JSON 参数 + json_each），在 MATERIALIZED scoped CTE 内、距离/top-k 前筛选 canonical 树 URI 且 fact_id IS NULL。必要性：只在 top-k 后过滤会让历史 F 卡挤掉合法叶；本棒不做历史清扫，须在候选阶段隔离。关闭态不传此参数，SQL 条件、参数及返回结构保持原样。
- tests/test_facts_tree_transactions.py：两例首次失败参数断言改为 projection_status 等于事务前状态，再证 search 空且不被 stale 阻断；失败更新断言同样比较事务前状态，并证上一版 search 可命中。所有原子计数、连接状态、上一版字节断言保留。
- tests/test_facts_tree_stale.py：新增 7 个参数化实例，真实 provider/sqlite-vec + 真实 ledger fixture + 离线 Embedding。
- 本报告。

## 为什么保留触发器

facts 的 insert/update/delete stale 触发器仍属于既有关闭态自动投影机制；门开只让该遗留状态对 search/query 无害，不动 schema/表/触发器/迁移。门开重建不借 stale 刷回旧卡。段 R 失败回滚恢复原有 stale 是正确事务行为，不应强迫 None/ready。

## 新增测试（未执行）

- test_open_search_empty_never_rebuilds_or_creates_atomic_cards（3实例：stale、带错误 stale、无 state）：sentinel 禁止自动 rebuild/_fact_document；无树时 search/query 空，documents/chunks/state 快照零变化。
- test_open_search_and_query_hit_only_existing_tree：插 fact 后既有 stale 保持，发叶后 search/query 仅该叶；读操作不写、不造原子卡。
- test_open_explicit_rebuild_only_reprojects_tree：清空树 chunks 后显式重建恢复检索，sentinel 禁止 _fact_rows/_fact_document，零原子卡文档，遗留 state 不变。
- test_closed_stale_search_still_rebuilds_atomic_cards：关闭态 stale 检索仍自动重建并返回 F1，query 仍列 F1，状态 ready。
- test_open_mixed_history_hidden_before_top_k_without_cleanup：先关闭态制造旧 F chunks 与 brief，再门开发叶；top_k=1 仍只命中树叶，query 同样仅树；读操作零写；显式 rebuild 不清理旧块，仍不显示它们。

## 静态自证与边界

`.venv/bin/python -m compileall -q app scripts tests/test_facts_tree_transactions.py tests/test_facts_tree_stale.py`：exit 0。
`git diff --check`：exit 0。
关闭态 golden、tests/test_document_read.py、skills、skill-registry、tool-registry、initialize.py/schema 均无本棒改动。
段 R 的 leaf_store/rollup 未再次修改；工作区中其 diff 属段 R 保留改动。
harness-openclaw/DREAMS.md 与 dreaming/session-corpus 等既有脏文件未动、未 stage；19/20 接单档不加入提交。

pytest 未运行，不能声称 0 failed 或提供全量终态数字；新增7例及修改后的R14例均交饭团正常 shell 复跑。停止本棒，不提交。
