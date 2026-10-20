# 事实树 B3b 交接详档 · 门开记忆层切换（原子卡/旧简报退出记忆层）

- 起点 HEAD：`8aeccd2`（B3a 完成，基线 **1341 passed / 1 skipped / 0 failed**）。
- 本棒性质：**行为变更**，但全程在 `app/settings.py:SAM_FACTS_TREE` 开关门后；关闭态 `document_read` 主流水线产物与 state 逐字节不变。
- 设计依据：v1.2 §1.2（原子卡不进记忆层、事实只留 SQLite）、§8（叶子→trunk/tree 聚合）、§12 验收 5/8。
- 铁律：零业务词/正则/关键词/枚举/写死公式；只用临时库；只本地 commit，不 push/部署；harness 脏文件勿 stage；遇真实口径冲突停手列点。

---

## 0. 本棒只做一件事

门**开**时，一次文件处理的"记忆层写入面"从旧三件套切换为事实树：

| 记忆层产物 | 关闭态（现状，不变） | 门开态（本棒） |
|---|---|---|
| 原子事实卡 md（`2b_L2/F0001…`，经 `L2ProjectionService.publish` → `rebuild_projection`） | 写 | **不写** |
| 旧公司/模块 L1 brief（`L1BriefService.rebuild`） | 写 | **不写** |
| 旧 L0 guide（`L0GuideService.rebuild`） | 写 | **不写** |
| 叶子 + trunk/tree brief/guide（`_run_leaves` → rollup，B2b2 已接线） | 不写 | 写（保持） |
| SQLite `facts` 等结构化正本 | 写 | **照常写，一字节不改** |

关键：**不删表、不删向量代码、不做历史库迁移/清扫**。门开只针对"本次处理新写什么"。历史库里可能已有的旧原子卡 md 本期不清（全新建档/临时库天然没有）；检索面因此在全新公司上天然只剩叶子。不实现"开闸时清扫存量"。

## 0.1 已核实的真实接线点（以代码为准，勿臆造）

- `app/document_read/service.py`
  - 第 **199** 行：`L2ProjectionService(self.db_path, self.memory).publish(company_id=company_id)` —— 旧原子卡投影入口。
  - 第 **200-201** 行：`if settings.SAM_FACTS_TREE: self._run_leaves(...)` —— 事实树叶子阶段（已存在，保持）。
  - 第 **228-243** 行：`if state['fact_ids']: if not state['brief_done']: L1BriefService(...).rebuild(...)` 与 `if not state['guide_done']: L0GuideService(...).rebuild(...)` —— 旧 brief/guide 入口。
  - state 持久化于 `sam://kv/rt/md-direct/<job_id>`，含 `brief_done / guide_done / leaves_done` 等布尔键；`_finish` 落终态。
- `app/memory/company_facts/l2_projection.py`
  - `L2ProjectionService.publish`：有 `rebuild_projection` 时整体重建 m1 向量投影（把 SQLite facts 全量投成原子卡 md + 向量分块）；否则逐 fact `memory.put(fact_uri(...), layer=2b_L2)`。
- 读侧不受影响（已核实）：`app/cloud_export.py:build_home_snapshot` 的 `facts = ConsolidationService(config.main_db).list_facts(...)`、`todos = list_todos(...)` **直接读 SQLite**，不依赖记忆层原子卡 md。故门开不投影原子卡不会让首页 facts 变空。
- 叶子/trunk/tree 已在 B1/B2b2 落盘到 `sam://facts/<company>/…`（rollup 调 `publish_pair`），同检索根，写入即被 m1 索引。

## 1. 改动清单

### 1.1 门开跳过旧原子卡投影（service.py:199）
- 改为：关闭态维持 `L2ProjectionService(...).publish(...)`；门开态**不调用**它。
- 门开态 SQLite facts 在第 188-198 行的 `consolidate_candidates` 短事务里已提交，保持不变。
- 不修改 `L2ProjectionService` 本身（关闭路径仍用它，保持其测试与行为）。

### 1.2 门开跳过旧 L1 brief / L0 guide（service.py:228-243）
- 现逻辑：`if state['fact_ids']:` 后按 `brief_done/guide_done` 跑旧 L1/L0。
- 改为：该旧 brief/guide 整块仅在**关闭态**执行。门开态不调用 `L1BriefService/L0GuideService`、不产生旧 brief/guide 调用预算（`brief/guide` 两个 action 门开不发生）。
- state 兼容性：门开态不要把 `brief_done/guide_done` 写成 True（避免语义谎称旧产物已建）。保持缺省/False 即可，因为门开态根本不读这两个键去建旧件；`_run_leaves` 的 `leaves_done` 才是门开态的完成标志。`state['done']=True` 与终态 message 门开仍正常落。
  - 若为了"断点续跑判定 done"必须有稳定布尔，可新增门开专属键（如 `legacy_skipped`），但优先不新增；确认现有 `done` 判定不依赖 brief_done/guide_done 后用最小改法。先读 `run()` 对 done/续跑的判定再定，不确定就停手。

### 1.3 预算/阶段（service.py）
- 门开态旧 brief/guide 不产生调用，现有预算相 `classify/attribute/extract/plan/leaf` 保持；**不新增、不重命名**预算相。
- 预算花完（`BudgetStopped`）门开仍落 partial，叶子逐叶 checkpoint 不变。

### 1.4 不改的东西
- 不改 `l2_projection.py / l1_brief.py / l0_guide.py / provider.py / schema.py`。
- 不改 facts 表、consolidation、分类、解析、任何员工 prompt/技能。
- 不新增依赖；新增 import 仅允许 app 内部模块。

## 2. 测试要求（迁移既有门开预期 + 新增门开断言）

复用现有 document_read 测试夹具（`tests/test_document_read.py` 的 `task/Model/service` 关闭态夹具，`tests/test_document_read_leaves.py` 的 `open_task/LeafModel` 门开态夹具）。先读这两个文件真实构造再改，勿臆造签名。

### 2.0 既有门开测试预期迁移（B2b2 是"门开也跑旧简报"的过渡形态，本棒有意改掉）
`tests/test_document_read_leaves.py` 中门开用例当前断言 actions 尾部仍含 `'brief','guide'`，本棒后这些**门开**断言要去掉 `'brief','guide'`（并去掉对 brief_done/guide_done 为 True 的期待，门开态这两个键保持初始 False）。已核实需迁移的用例/断言（以实际文件为准，逐一改，勿整文件删）：
- `test_open_complete_same_channel_and_order`：门开 actions 变为 `['classify','attribute','extract','plan','leaf']`；续跑断言同步；`assert run(reader)==result` 幂等仍成立。
- `test_leaf_budget_checkpoint_resume_without_duplicate`：续跑后 actions 末尾不再有 `'brief','guide'`（应为 `…,'plan','leaf','leaf'`）。
- `test_gate_open_resume_old_partial_state_adds_new_call_phases`：开门续跑后 actions 去掉尾部 `'brief','guide'`。
- `test_leaves_done_resume_only_brief_guide`：该用例语义在门开态已不成立（门开不补旧简报）。将门开部分改为断言"leaves_done 后续跑是 no-op、actions 不新增"；若该用例同时承担关闭态覆盖则保留关闭态分支。据实际代码最小改，不确定就停手问。
- 其它门开用例（如 162/172/190/271 行等）凡门开 actions 末尾带 `'brief','guide'` 的，统一去掉这两项；**但不得改动对 leaves/rollup/预算/断点的既有断言**。

### 2.1 关闭态一字不动（硬门禁）
- `tests/test_document_read.py` 全部关闭态用例（含 68 行 `actions==['classify','attribute','extract','brief','guide']`、92/227 行等）保持不变且必须继续通过。
- `tests/test_document_read_leaves.py` 里的**关闭态**用例（约 230-256 行，`monkeypatch SAM_FACTS_TREE False`、对 `tests/fixtures/document_read_closed_b2b1.json` 做 state_json/documents **逐字节比对**那条）保持不变；不得改 golden、不得改其 actions 与 docs 断言。

1. **关闭态逐字节回归（硬门禁）**：
   - 默认（`SAM_FACTS_TREE` 未开）跑一次 `run()`，断言 Model 记录的 actions 仍包含旧 `l2` 投影相关调用与 `brief/guide` 调用（按现有测试口径），state JSON 与现有 golden/断言逐字节一致；已有 B2b2 golden 不得改动。
2. **门开不投影原子卡**：
   - 开门 + 假员工（plan/leaf 给最小合法产出，坐标用带命名空间的真实形态如 `docx:body/16/table`）跑通后，断言：
     - `memory` 中**不存在**任何 `2b_L2`/`F####` 原子卡 URI（可用 provider query/prefix 或直接列 m1_documents 断言无 layer=2b_L2 的 fact 投影）；
     - Model actions **不含** brief/guide 旧 action；
     - SQLite `facts` 表仍有本文件核验事实（正本在）；
     - 叶子文件与该 trunk 的 trunk-brief/trunk-guide、根 tree-brief/tree-guide 已生成且内容非空。
3. **门开断点续跑**：预算在 leaf 中途停止 → partial；提高预算续跑，不重建已建叶、不补写旧原子卡/旧 brief，最终 done 且记忆层无原子卡。
4. **首页读侧不塌**：开门态跑完后，用 `ConsolidationService(db).list_facts(company_id)` 仍能读到事实（证明跳过的只是记忆层投影，不是事实本身）。
5. 开关在进程内用现有测试惯用方式切换（monkeypatch `app.document_read.service.settings` 或项目既有开关测试手法，与 B2b2 测试一致），测完还原，不得留下全局污染。

## 3. 量化验收
- compileall + 全量 pytest：基线 1341 passed/1 skipped 只增不减，0 failed/error；报告新增测试数与终态数。
- `git diff --check` 干净；diff 中无业务词/正则/枚举写死；无新第三方依赖。
- 关闭态：service.py 旧产物路径与 B2b2 golden 逐字节不变。
- 门开态：记忆层写入口仅 facts-tree 一条（leaf/trunk/tree），SQLite 正本照常。

## 4. 交付边界
只做 B3b，停手等饭团真机黑盒；不 push/部署；不进真机真模型（B3c 由饭团用鸿通晟原件跑）；harness-openclaw 脏文件勿 stage、勿改 catalog/tool-registry。完工报告写 `docs/handoffs/facts-tree-segment-b3b-report.md`：改了哪些文件、新测试清單、关闭态/门开态实测数字。
