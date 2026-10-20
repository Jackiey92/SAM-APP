# 事实树 · 段P（真机投影通道修复）交接档

> 承接 16（B3b，`a891ed3`）。B3c 真机端到端首跑暴露：**事实树产物从未真正写进过真机向量记忆层**，B1~B3b 全绿是因为单测用零校验假 Memory。本棒只修真机 `LocalVectorMemoryProvider` 的事实投影通道，不改事实树内核（leaf_store/rollup）与任何护栏。

- 唯一入口：`crisp`。UTF-8 中文 prompt 文件，禁止内联。
- 遇**口径/签名冲突即停手列点**，不自行猜测；这是被鼓励的纪律。
- 硬边界：**不改** `app/facts_tree/leaf_store.py`、`rollup.py`、`leaf_build.py`、`leaf_plan.py` 的对外行为与护栏；**只改真机存储适配**（主要 `app/memory/provider.py`）+ 新增真机 provider 级测试。不接 UI、不调真模型、不 push/部署。
- 默认 `SAM_FACTS_TREE=False`：关闭态行为/产物/字节与现在完全一致，旧事实卡（`sam://facts/<co>/<英文code>/F*.md`）路径不受影响。
- 只在临时隔离库验证；不写业务正则/关键词/枚举；中文目录↔英文 code 的对应只能复用 `app/module_tree.py:MODULES` 单一事实源，不得另立映射。

## 0. 签名核对（派单前已对真实代码核过，仍请你先复核）

- 报错实栈（B3c 真机，门开、已 register generic，真模型五相跑到写叶）：
  ```
  leaf_store.py:431  previous = read_optional(self.memory, uri)
  l1_brief.py:37     return memory.read(uri)
  provider.py:349 → _address(uri)
  provider.py:82     raise ValueError("unknown fact-memory module")
  ```
  触发 URI 形如 `sam://facts/<co>/02-业务概览/leaves/L-bizov-01-….md`。

### 0.1 两层通道缺口（都已读代码确认）

1. **读/删层** `provider.py:74-82 _address()`：
   ```python
   address = paths.parse(uri, company_id=self.company_id)
   ...
   if address.module_key not in MODULE_KEYS | {UNCLASSIFIED, TODO_DIRECTORY, ""}:
       raise ValueError("unknown fact-memory module")
   ```
   `paths.parse`（`app/memory/paths.py:76`）把 `parts[1]` 直接当 `module_key`。事实树 URI 第二段是**中文目录**（`02-业务概览`），不在英文 code 白名单 → read 被拒；read 在 write 之前（读上一版做 no-op 比对）就炸。
   - `read()` 在 `provider.py:341-360`；`delete()` 在 `provider.py:477` 同样过 `_address`（publish_pair 失败补偿会调 delete，必须一起支持）。
   - 公司根文档 `tree-brief.md/tree-guide.md/tree.meta` 的 `module_key=""` 已能过；本棒保持。

2. **写层** `provider.py:294-330 publish_documents()`：
   ```python
   if addresses[uri].parts[-1] not in {"brief.json","brief.md","guide.json","guide.md"}:
       raise PermissionError("bundle only accepts brief/guide projections")
   ```
   即使修好模块名校验，事实树写入也会被这个旧白名单拒：
   - 叶子文件名 `L-<key>-<NN>-<slug>.md`
   - 树枝简报 `trunk-brief.md` / `trunk-guide.md`（在中文目录下，见 `paths.trunk_doc_uri`）
   - 公司简报 `tree-brief.md` / `tree-guide.md`（公司根）
   - 冲突件 `待办与冲突/conflict-<trunk>-<metric>-<jobid>.md`（`paths.conflict_uri`，`module_tree.TODO_DIRECTORY="待办与冲突"`）

3. 事实树侧全部走**通用口**，不要让本棒改这些调用：
   - `leaf_store.py:19` import；`:431` `read_optional`；`:463` `publish_pair(..., metadata={'company_id','owner_skill':'fact-leaf'})`；`:470` 失败补偿 publish_pair(None)。
   - `rollup.py:5/39/67/69` 同样 read_optional/publish_pair；trunk URI 见 `:100-101/113-114`，tree rollup 另产出 tree-brief/guide。
   - `publish_pair`（`memory/company_facts/l1_brief.py:43`）：有 `publish_documents` 属性就调它，否则退化为逐文档 put/read/delete 补偿。真机 provider 必须在 `publish_documents` 这一层接住事实树族（这样 leaf_store/rollup 零改动）。

4. 段A预留的 `provider.publish_leaves()`（`provider.py:234-291`）**当前无调用方**，只支持 leaves、不含 trunk/tree/冲突件。它内部已有可复用的严格校验（`modules={m.directory:m.key}`、parts 长度、`L-key-NN`、canonical URI、单 company scope、事务回滚/stale）。**可复用其校验/落库逻辑，但不要让 leaf_store 改去调它**——目标是通用 `publish_documents`/`read`/`delete` 通道在门开时识别完整事实树族。

5. 漏检根因：`tests/test_document_read.py:15-22` 的假 `Memory` 只有 `docs` dict、put/read/delete 零校验；`tests/test_facts_tree_*.py` 也都用假 provider。**真机 provider 从未在事实树 URI 上被测过**。本棒必须补真机 provider 级测试（见 §3）。

## 1. 本棒目标（一句话）

门开时，真机 `LocalVectorMemoryProvider` 的 `read / publish_documents / delete` 完整支持事实树 URI 族（叶子、树枝简报导览、公司简报导览、冲突件），写入 `m1_documents`（`fact_json IS NULL`）并经现有 `_project` 进向量检索面，可读回、可回滚；关闭态与旧事实卡路径逐字节/逐行为不变。

## 2. 硬约束

1. **门控**：新识别逻辑只在 `require_enabled()` / `settings.SAM_FACTS_TREE` 门后生效。关闭态 `_address`、`publish_documents` 白名单、read/delete 行为与现状完全一致；现有 golden/关闭态测试一字不改、必须仍绿。
2. **单一事实源**：中文目录 ↔ 英文 key 一律 `{m.directory: m.key}` / `{m.key: m.directory}` from `app/module_tree.py:MODULES`；系统枝用 `TODO_DIRECTORY/UNCLASSIFIED`。禁止新写正则、关键词、枚举、写死中文列表。
3. **形态严格校验（防任意写/路径注入）**，强度不低于 `publish_leaves` 现有实现：
   - 叶子：`parts == (<co>, <已知目录>, "leaves", <file>)`；file 形如 `L-<key>-<NN>-<slug>.md`，其中 `<key>` 必须等于该目录映射的英文 key、`<NN>` 两位非 00 数字；用 `paths.leaf_uri(...)` 反算 canonical URI 必须与输入完全相等。
   - 树枝简报：`(<co>, <已知目录>, trunk-brief.md|trunk-guide.md)`（以 `paths.trunk_doc_uri` 反算为准）。
   - 公司简报：`(<co>, tree-brief.md|tree-guide.md)`；`tree.meta` 已另有独立投影表/方法，本棒不把它当普通文档写。
   - 冲突件：`(<co>, 待办与冲突, conflict-*.md)`，沿用 leaf_store 生成的 canonical 名（可由 `paths.conflict_uri` 反算）；不得借冲突目录写任意文件。
   - 任何不认识、越权 company、非 canonical 的 URI：门开也必须拒绝（ValueError/PermissionError），不得"放行未知"。
4. **落库 module_key 用英文 key**：写 `m1_documents.module_key` 时，中文目录行存对应英文 key（与 `publish_leaves` 现有做法一致）；tree-brief/guide 用 `""`；冲突件用系统枝常量。保证既有按 module_key 的检索/过滤不破。
5. 事务原子性、`BEGIN IMMEDIATE`、失败 `_stale` + 回滚、成功置 ready 的语义，与现有 `publish_documents` 完全一致；失败不得留下半批文档。
6. 旧事实卡（`<英文code>/F*.md`，经 `_fact_rows` 动态投影）与旧 `brief.md/guide.md/brief.json/guide.json` 模块投影路径**保持原样可读写**；门开只是"新增接受事实树族"，不是改走旧文件的语义。
7. 不新增第三方依赖；不改 harness-openclaw/、catalog/tool-registry；不改任何 SKILL。
8. 不调真模型；不 push/部署；遇 `.git/index.lock Read-only file system` 则保留暂存、停手报告，由饭团在正常 shell 提交（与以往相同）。

## 3. 必须新增的测试（真机 provider 级，关键）

新建（或在既有 provider 测试中扩展）**用真实 `LocalVectorMemoryProvider` + 临时库 + 临时 memory_root** 的测试，门开：

1. 写一片合法叶子（含脚注的最小合法 markdown）→ `read(uri)` 字节一致；`m1_documents` 行 `module_key`=英文 key、`fact_json IS NULL`；`m1_chunks` 有该叶子向量块（证明进检索面）。
2. 写 `trunk-brief.md/trunk-guide.md`（中文目录下）与 `tree-brief.md/tree-guide.md`（公司根）→ 全部可读回；tree 根 `module_key=""`。
3. 写冲突件 `待办与冲突/conflict-*.md` → 可读回。
4. no-op/重复写：同内容再写，版本/字节行为与 leaf_store 既有 no-op 语义一致（leaf_store 层已有测试，这里只需保证 provider 层 upsert 不产生重复行）。
5. 删除/回滚补偿：publish_pair 对某 URI 传 None（前值存在）后 read 抛 FileNotFoundError，m1_documents/m1_chunks 对应行删除。
6. **安全拒绝**：门开下，中文目录写错 key（`02-业务概览/leaves/L-hr-01-x.md`）、非两位/00 编号、非 canonical（加 `../`、多余段、伪造 slug）、未知中文目录、跨 company metadata、往 `待办与冲突` 写非 conflict 名 —— 全部必须抛错且不落任何行。
7. **关闭态对照**：`SAM_FACTS_TREE=False` 下，同样的事实树 URI 在 `read/publish_documents/delete` 仍按现状被拒（`unknown fact-memory module` / 旧白名单 PermissionError）；旧事实卡与旧 brief/guide 读写测试全部仍绿；现有关闭态 golden 不变。
8. 回归：现有 `tests/test_document_read*.py`、`tests/test_facts_tree_*.py` 全绿（它们用假 provider，不应受影响），全量只增不减、0 failed。

> 注：这些新测试**不能**再用零校验假 Memory，必须构造真机 provider（参考仓库里既有的真机 provider 测试夹具；如无现成夹具，用 `connect(临时db)` + 真机 embeddings 的测试替身或现有 provider 测试的构造方式，保持与仓库既有真机测试一致，不要为此引入网络/真模型）。

## 4. 交付物 / 自证 / 验收

- 交付：真机 provider 事实树投影通道修复 + §3 测试；如对段A未接线的 `publish_leaves` 做了复用/收敛，在报告里说明最终是"扩展 publish_documents"还是"新增内部事实树投影助手 + publish_pair 路由"，以及为何不动 leaf_store/rollup。
- 自证：全量 `pytest -q` 收尾行（只增不减、0 failed/0 error）、`python -m compileall app scripts`、`git diff --check`；关闭态 golden 与旧测试未改的证据；`git status` 说明未碰 harness、catalog、tool-registry、SKILL。
- 报告：`docs/handoffs/facts-tree-segment-p-report.md`，列改动文件、新增测试数、每个 §3 用例对应的测试名、以及关闭态不变的证据。
- 停手点：若发现 leaf_store/rollup 的调用契约本身与真机 provider 无法在不改内核的前提下对齐，或真机 provider 夹具无法离线构造，**停手列点**，不要硬改内核或引网络。

## 5. 饭团黑盒（你不需要做）

- 独立复跑全量；逐行审 provider diff（确认无业务正则/关键词、中文目录映射只来自 MODULES、关闭态零变化）。
- 复跑 B3c 真机脚本（门开、register generic、真实 docx 全链路），验叶子/trunk/tree 真正落真机记忆层、可检索、纳米句进入业务叶、坐标可回查、数字不漂。

做完停在本棒，不自行进入下一步。
