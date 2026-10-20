# 事实树 B3a 交接详档：叶子级显隐（废除整模块 hidden）+ 建档绑定回归

给 CRIS。本棒在 B2b2（`c4191d9`，1317 passed）之上。权威 `09` v1.2 §9（前台显隐）、§10（注册绑定）、§12.11/§12.12（验收）。本棒仍是**默认开关关闭、零真模型、零前端改动**。

## 0. 铁律（不变）
- 只本地 commit 不 push 不部署；不 stage `harness-openclaw/` 无关脏文件；零新依赖；后端零业务规则（显隐只能反映"是否存在有内容的 active 叶子"这一存储事实，不得出现关键词/行业词/枚举归类）。
- **关闭态逐字节不变**：cloud_export 产出的 `visible_modules`、cloud_home 的 fallback 在门关闭时必须与现在完全一致（现 5 个 built-in hidden 仍隐藏）。必须有字节级/结构级回归。
- 只测试库；遇口径冲突停手列点。

## 0.1 已核对的现状（直接照用）
- 树枝可见性当前两处决定，都是静态：
  - `app/cloud_export.py:53` 快照产出：`'visible_modules': module_definitions(include_hidden=False)`（import 自 `..module_taxonomy`，shim 到 `module_tree`）。
  - `app/cloud_home.py:45` 云端只读 fallback：`"visible_modules": module_definitions(include_hidden=False)`。
  - `app/module_tree.py`：`Module(key,directory,order,hidden)`；prod/purch/sales/fin/fund 五项 `hidden=True`；`module_definitions(*, include_hidden=True)` 返回 `{code,name,sort_order,hidden}`。
- active 叶子枚举已存在：`app/facts_tree/rollup.py:FactsTreeRollup._active(company, trunk=None)`——遍历 `facts_tree_leaf_index` 的 `L-` 前缀叶子、读 uri 内容、与 `leaf_revisions` 最新 `content_sha` 校验一致、`parse_markdown` 后要求 `status=='active'` 且（概览/正文有 prose 或 数据明细表有行）；冲突/归档/空壳不计。`store.binding.get(company)` 未绑定会 `LeafRejected`。
- 叶子 trunk 在 frontmatter `meta['trunk']`（如 bizov），code 形如 `L-bizov-07`。
- 建档绑定段A已实现并测过：`app/facts_tree/company_binding.py:CompanyBindingStore.register(company_id, display_name, pack="generic")`、`.bind_pack(...)`（空仓可换、写后锁）、`.get(company_id)`；register 内部 `require_enabled()`。本棒**不改**这些逻辑，只补回归确认。
- cloud_export 是纯快照装配（`build_home_snapshot(*, company_id)`，无模型/无写）；cloud_home 从快照 `page_context` 读，读不到才静态 fallback。
- 前端模板不在本仓；本棒只产出快照字段，不改任何模板。

## 1. 目标
1. 新增一个**纯存储只读**的叶子级显隐查询（放 `app/facts_tree/`，建议 `visibility.py`），给出"某公司当前有哪些树枝存在有内容的 active 叶子"。
2. 门开时，cloud_export 的 `visible_modules` 改为**叶子级动态**结果（覆盖 §9：废除整模块 hidden，有 active 叶即显示，哪怕是现 built-in hidden 的 prod/purch/sales/fin/fund）；结构保持 `{code,name,sort_order,hidden}` 字段不变以免破坏消费方，`hidden` 字段在门开动态结果里表示"当前是否有 active 叶"的反面（即无 active 叶→hidden=true），`name/sort_order` 仍取自 `module_tree`（结构元数据，不是业务判断）。
3. cloud_home 云端 fallback：门在云端通常关闭（只读），保持现状不动；若门开且快照缺该字段才允许走同一动态函数（需有 memory/db 可读，实际云端拿不到就保持静态）。**默认路径零变化**。
4. 建档绑定回归（不新增产品逻辑）：用现有 `CompanyBindingStore.register/bind_pack` 写一组针对 §12.12 的 facts_tree 层断言。

## 2. 施工要求

### 2.1 `app/facts_tree/visibility.py`（新文件，纯只读护栏）
- `active_trunks(*, db_path, memory, company_id) -> list[str]`（或返回带 sort 的结构）：
  - 内部 `require_enabled()`（门关闭不调用即可，但函数自身也守）。
  - 复用 `LeafPublishService` + `FactsTreeRollup(store)._active(company)` 拿到全部有内容 active 叶子（trunk 过滤传 None）；从叶子 `meta['trunk']` 收集去重 trunk。**不要**重写索引/SHA/frontmatter 校验逻辑。
  - 未绑定模板的公司：返回空列表（不抛到快照层），由调用方决定静态/动态。
  - 只读、不写 memory、不调模型；结束 close store。
- `visible_modules_for_company(*, db_path, memory, company_id) -> list[dict]`：以 `module_tree.MODULES` 全量 10 枝的 `code/name(directory)/sort_order(order)` 为底，`hidden = code not in active_trunks(...)`。系统枝（待办与冲突/待归类）不进 visible_modules（它们只在待办中心）。无 active 叶的树枝 hidden=true、有则 false。

### 2.2 cloud_export 接线（仅门开）
- `build_home_snapshot` 内 `visible_modules` 赋值处包一层：
  - 关闭：`module_definitions(include_hidden=False)`，**与现状逐字节一致**。
  - 门开：`visible_modules_for_company(db_path=config.main_db, memory=memory, company_id=company_id)`（memory 已在函数内构造，复用同一实例）。
- 门开分支不得触发模型调用、写操作、分类器；只是多读 facts_tree 索引与叶子内容（与 rollup 同源只读）。
- 不改 page_context 其它字段、不改 SNAPSHOT_VERSION 语义（如需 bump 先在报告说明理由；倾向不 bump，字段结构没变）。

### 2.3 cloud_home
- 默认 fallback 保持静态 `module_definitions(include_hidden=False)` 不变（云端只读、拿不到主仓 facts_tree）。不要为了门开给云端引入 db/memory 依赖；如确要，最小化并在报告说明。本棒验收以"默认路径不变"为准。

### 2.4 建档绑定回归（§12.12，纯 store 层，不新增 API）
在 facts_tree 测试文件（可并入现有 plan/build 测试文件或新建 test_facts_tree_visibility.py）用临时库断言：
- 门开 `register` 写 tree.meta（经 projection），返回 template_pack/pack_version；重复 register 拒绝。
- 注册后不预生成任何叶子文件（facts_tree_leaf_index 为空、sam://facts 下无 leaves）。
- 空仓（无叶子/无 facts）`bind_pack` 换 generic 允许（本期只有 generic，断言同包重绑/版本一致路径不报错即可；不要假造不存在的垂类包）。
- 写入任一 active 叶子后再 `bind_pack` 必须 `PermissionError`（locked/nonempty）。
- 门关闭时 `register/bind_pack` 被 `require_enabled` 拒绝。

## 3. 测试（tmp_path，不连真模型）
1. 显隐纯函数：空树 → active_trunks=[]、visible_modules 全 10 枝 hidden=true（且不含系统枝）；用现有叶子发布夹具在 prod（现 built-in hidden）发一片有内容 active 叶 → 门开 visible_modules 里 prod hidden=false，其它无叶枝 hidden=true；归档/冲突/空壳叶不使其出现。
2. 动态随内容：再给 sales 发一片 → prod、sales 都可见；删除/归档到无 active 叶后重新计算又隐藏（本棒只读计算，删除路径以现有 store 能力为准，没有删除就用"全部 archived"构造）。
3. cloud_export：门关闭时 build_home_snapshot 的 visible_modules 等于旧静态结果（结构/字段/顺序一致，建议对期望 list 做等值断言）；门开且某 hidden 枝有 active 叶时该枝出现在快照可见集。
4. cloud_home 默认（门关闭）fallback 不变。
5. §2.4 建档绑定四条断言。
6. 未绑定公司门开：visible_modules 走动态得到全 hidden（不抛异常），快照仍可生成。

## 4. 验收硬标准
- 全量在 **1317 passed / 1 skipped** 上只增不减、0 failed；compileall、git diff --check；零新依赖；后端无行业词/关键词。
- 关闭态 cloud_export/cloud_home visible_modules 与现行为完全一致（专门等值断言）。
- 不改前端、不改 SNAPSHOT_VERSION（除非报告充分说明）；不接真模型；harness 无关脏文件不 stage；只读沙箱则保留暂存报告写明。
- 回执 `docs/handoffs/facts-tree-segment-b3a-report.md`：新 API、门分支、显隐用例、关闭态等值证据、建档绑定四条结果、未做项（B3b 记忆层切换/B3c 真机留后）。停手等黑盒，不进 B3b。
