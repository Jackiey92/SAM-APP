# 段A 交接详档：事实树骨架（纯增量重构，零行为变化）

> 唯一设计权威：`docs/architecture/09-facts-tree-design.md`（v1.2）。本文件只把其中**段A**要落地的部分转成工程清单；如本文件与 v1.2 冲突，以 v1.2 为准并停下来在交付报告里指出，不要自行解释。
> 段A性质：**纯增量 + 改名 + 只读注册表**。不接入上传/解析/投影主流程，不改任何现有接口语义。验收的头号标准是“系统行为一个字节都不变”。

---

## 0. 背景关键事实（饭团已尽调，直接采信，不必重新摸排）

- 基线：`.venv/bin/python -m pytest -q -p no:cacheprovider` 当前 **990 passed, 1 skipped**（约 130s）。段A结束后必须仍是这个数（段A自身新增测试另算，全部通过）。
- 现有 10 树枝定义在 `app/module_taxonomy.py`（`Module(key,directory,order,hidden)` + `MODULES` + `MODULE_KEYS` + `TODO_DIRECTORY="待办与冲突"` + `UNCLASSIFIED="待归类"` + `validated_module` + `projection_module` + `module_definitions`）。
- 兼容转发文件 `app/memory/taxonomy.py` 从 `..module_taxonomy` re-export 同名符号。
- 现有消费方（改名后必须全部仍可 import、行为不变）：`app/classifier/{content_classifier,file_classifier,semantic_folders}.py`、`app/cloud_export.py`、`app/cloud_home.py`、`app/db/schema.py`、`app/home_shell.py`、`app/knowledge.py`、`app/memory/company_facts/{l2_consolidation,l2_fact_verifier,module_projection,paths}.py`、`app/memory/taxonomy.py`、`app/memory_map.py`。
- 记忆层 URI 唯一边界在 `app/memory/paths.py`：`FACTS_ROOT="sam://facts/"`、`parse()`、`facts_uri()`、`fact_uri()`（后者拼 `F<id>.md`）。
- 记忆 Provider：协议 `app/ports.py:MemoryProvider`；向量实现 `app/memory/provider.py:LocalVectorMemoryProvider`，其中
  - `put()`（约 184 行）对 `F<数字>.md` 或 `metadata.layer=="2b_L2"` 直接 `PermissionError`；
  - `publish_documents()`（约 38 行）有 bundle 白名单，当前只允许文件名 ∈ {brief.json,brief.md,guide.json,guide.md}；
  - `rebuild_projection()`（约 424 行）会把原子卡也投影进 `m1_documents`（段B要处理，段A**不要动**）。
- SQLite schema 在 `app/db/schema.py`（`CREATE TABLE` 全部 `IF NOT EXISTS`）。
- 已确认 `.venv` 内有 PyYAML 6.0.3（可 `import yaml`，**禁止再新增任何依赖**）。
- 98 叶数据包已由饭团从 v1.2 §3 权威表生成：`app/facts_tree/templates/generic/leaves.yaml`（已在工作区，含 `pack/version/is_industry_pack/trunks/leaves`，98 条 `code: L-...`，段A要把它纳入版本库，不要重抄、不要改数）。

---

## 1. 段A 交付条目

### 1.1 模块树改名（taxonomy → tree），旧名全部保留为兼容壳

1. 新建 `app/module_tree.py`，把现 `app/module_taxonomy.py` 的**全部内容原样搬迁**（含 `hidden` 字段与 `module_definitions(include_hidden=...)` 行为，段A一个行为都不改）。文件 docstring 用树语言描述：树枝=module、目录=树枝目录名。
2. `app/module_taxonomy.py` 改为**纯兼容 shim**：只从 `.module_tree` re-export 全部公共符号（`Module,MODULES,MODULE_KEYS,TODO_DIRECTORY,UNCLASSIFIED,validated_module,projection_module,module_definitions`），顶部加注释“兼容旧导入路径，新代码请用 app.module_tree；勿在此加逻辑”。**不得改变任何符号的对象身份/返回值**。
3. `app/memory/taxonomy.py` 继续可用：把它的 import 源从 `..module_taxonomy` 改成 `..module_tree`（re-export 的符号集合保持不变）。
4. 其余业务文件**本段不要批量改 import**（旧路径仍可用，降低回归面）；仅当 linter/测试因双份定义出问题时才最小调整。目标：`grep -r module_taxonomy app` 里，真实定义只剩 shim 一处，其他是 import 或注释。
5. 新增/补齐测试 `tests/`（命名跟随现有约定）断言：
   - `app.module_tree` 与 `app.module_taxonomy` 导出的 `MODULES` 是同一批 code/order/directory/hidden，且 `len==10`；
   - 10 个 code 精确等于 `{info,bizov,hr,prod,purch,sales,mkt,fin,fund,legal}`；
   - `validated_module("info")=="info"`、`validated_module("nope") is None`、`projection_module(None)=="待归类"`；
   - `TODO_DIRECTORY=="待办与冲突"`、`UNCLASSIFIED=="待归类"`。

### 1.2 事实模板包只读注册表（可插拔的“包管理”部分，不接主流程）

新建包 `app/facts_tree/`：

- `app/facts_tree/__init__.py`
- `app/facts_tree/template_packs.py`：纯确定性加载/校验，**不含任何行业判断、关键词或枚举映射**。
  - 数据类 `LeafCode(code, trunk, title, shape, scope)`、`TemplatePack(pack, version, is_industry_pack, trunks, leaves)`。
  - `load_pack(name="generic") -> TemplatePack`：从 `app/facts_tree/templates/<name>/leaves.yaml` 用 PyYAML 读取（UTF-8）。
  - 强校验（不满足直接抛 `ValueError`，错误信息带 pack 名和违规 code）：
    1. 每条 leaf 的 `code` 形如 `L-<trunk>-<NN>`，`<trunk>` 必须是 10 个合法树枝 code 之一，`<NN>` 为两位数字；
    2. 同一树枝内 NN 从 01 起连续、不重号、不跳号；
    3. `shape` 只能是 `n` 或 `s`；
    4. 全 pack 叶子 code 全局唯一；
    5. generic 包必须正好 98 叶，各树枝计数精确为 info12/bizov15/hr9/prod9/purch4/sales18/mkt19/fin2/fund5/legal5；
    6. 叶子 `trunk` 与 code 前缀一致；`trunks` 段的 code/order/directory 与 `app.module_tree` 一致（directory 允许模板自带中文名，但 code/order 必须一致）。
  - `available_packs() -> list[str]`：列出 `templates/` 下含合法 `leaves.yaml` 的子目录名（按名排序）。
  - `validate_pack(name)`：供测试/未来垂类包预检，返回归一化后的 TemplatePack 或抛错。
  - 垂类包规则（只在此处实现为校验约束，不做行业逻辑）：叶子只能挂在既有 10 树枝下，不允许声明新 trunk code（否则抛错）。
- 数据文件 `app/facts_tree/templates/generic/leaves.yaml`（饭团已生成，纳入提交）。
- 单元测试：
  - generic 包加载成功且计数/连续性/唯一性全过；
  - 故意构造一个坏包（临时目录：跳号 / 非法 trunk / 坏 shape / 重复 code / 新 trunk，逐个用例）断言抛 `ValueError`；
  - `available_packs()` 至少包含 `"generic"`。

### 1.3 公司建档与模板绑定（只建表 + 服务 + 单元测试，不接 HTTP/UI、不接上传）

1. 在 `app/db/schema.py` 追加（全部 `CREATE TABLE IF NOT EXISTS`，不改动任何既有表）：
   ```sql
   CREATE TABLE IF NOT EXISTS company_tree_meta (
       company_id     TEXT PRIMARY KEY,
       display_name   TEXT NOT NULL,
       template_pack  TEXT NOT NULL,
       pack_version   INTEGER NOT NULL,
       bound_at       TEXT NOT NULL,
       locked         INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))
   );
   ```
   字段语义：`template_pack` 注册时绑定（段A只支持 `generic`）、`pack_version` 绑定当时模板版本、`locked` 一旦该公司写入事实置 1（段A只提供置位判定，不接写入流程）。
2. 新建 `app/facts_tree/company_binding.py`：`CompanyBindingStore`（接收 db 路径/连接，跟随现有 service 的写法与时间戳约定，UTC iso）。
   - `register(company_id, display_name, pack="generic")`：
     - 校验 company_id 满足 `app.memory.paths.identifier` 同款规则（字母数字与连字符、非空、≤128）；
     - 校验 pack 能 `load_pack` 成功；
     - 公司已存在则报错（不允许覆盖建档）；
     - 插入 `locked=0`、`pack_version=load_pack(pack).version`、`bound_at=now`。
   - `get(company_id) -> dict | None`。
   - `bind_pack(company_id, pack)`：**仅当 `locked=0` 且该公司记忆层为空**时允许改绑；否则抛 `PermissionError`。段A对“记忆层为空”的判定：调用注入的一个 `is_empty(company_id)->bool` 回调（构造参数传入；段A给一个默认“查 m1_documents/m1_chunks 是否有该公司任何文档行”的实现，但**不在任何主流程里调用它**，仅测试覆盖）。
   - `mark_locked(company_id)` 与 `is_locked(company_id)`：幂等置位/读取（段A不接线，仅测试）。
   - 只允许 `generic`：若传入其他 pack 名，`register` 抛 `ValueError`（为段C垂类包预留分支，但现在不开）。
3. 单元测试（用临时 db，不碰真实库）：
   - 注册→读回字段一致；重复注册报错；非法 company_id 报错；非法 pack 报错；
   - 空仓可 `bind_pack` 改绑成功；注入 `is_empty=lambda c: False` 后改绑抛 `PermissionError`；`locked=1` 后改绑抛 `PermissionError`；
   - `mark_locked` 幂等。

### 1.4 URI 能力预留（默认关闭，不改变现有路径行为）

目标：为段B的叶子/树级文件地址预留构造与放行能力，但段A**默认关闭**，任何现有调用路径的解析结果必须与改前逐字节一致。

1. 在 `app/memory/paths.py` 增加纯函数（不改 `parse()` 现有分支的返回）：
   - `leaf_uri(company_id, directory, file_name)`：形如 `sam://facts/<company>/<directory>/leaves/<file_name>.md`；`directory` 必须是某树枝目录名、`file_name` 满足现有 `_segment` 规则（禁止下划线/穿越），否则 `ValueError`。
   - `tree_meta_uri(company_id)`：`sam://facts/<company>/tree.meta`（**文件名无扩展名，以设计稿 v1.2 §1.2/§10.4 为准；内容仍是 JSON 文本**）。
   - `trunk_doc_uri(company_id, directory, doc)`：doc ∈ {trunk-brief.md,trunk-guide.md}，目录为树枝目录。
   - 这些函数在段A**不被任何写路径调用**，仅单测覆盖其拼出的 URI 能被现有 `parse()` 正常解析、company/module 归属正确。
2. 若现有 `parse()` 对 `.../leaves/<file>.md` 或根级 `tree.meta`（无扩展名，JSON 内容）解析失败，做**最小、向后兼容**的放宽（仅放行这些新形状，其余规则不变），并加测试证明：老的 5 类既有 URI（公司根、模块 brief/guide、F卡、original、kv）解析结果与放宽前完全一致（用一组快照式断言锁住）。注意 `parse()` 现有逻辑用“段里是否含 `.`”区分 module 与根文件，`tree.meta` 含点会被当成 module 段，需让它正确归到公司根（company 级、非 module），用测试锁死。
3. 增加环境开关 `SAM_FACTS_TREE`（段A只在 tests 中显式开启）：新路径构造函数在开关关闭时若被“用于写”应拒绝；段A没有写接线，主要靠测试保证开关默认关闭时主流程无任何新分支被走到。**不要在 settings 之外散落读取该变量**，集中在一个 settings 常量。
4. Provider：段A**不要修改** `publish_documents` 的白名单，也**不要删除**任何 F 卡/原子卡逻辑。若需要为段B预留，可以新增一个独立方法 `publish_leaves(...)`（内部复用现有事务/回滚/向量投影风格），但段A该方法不被调用，仅用直接单测验证其文件名/目录合法性校验与单公司 scope 校验；默认开关关闭时调用即抛 `RuntimeError("facts tree disabled")`。
5. `tree.meta` 读写归属：它是**公司根级受控元数据文件（JSON 文本，无扩展名）**，不是 brief/guide bundle，也不是叶子。段A给它一个独立、最小的受控写口（如 `put_tree_meta(company_id, payload: dict)` / `read_tree_meta(company_id)`），内部校验单公司 scope、payload 为可 JSON 序列化、字段仅含绑定信息（company 显示名、template_pack、pack_version、bound_at、locked）；普通 `put()` 仍不得直接写该 URI。该写口段A只被 `CompanyBindingStore` 与单测使用，不接主流程；默认开关关闭时拒绝写入。

> 说明：段B才会把“每树枝 trunk-brief/guide + 叶子 + tree-brief/guide”接入真实投影，并停掉原子卡投影；段A只把“地址能表达、开关默认关、写入口有闸”准备好。

---

## 2. 段A 绝不做（越界即返工）

- 不改任何现有接口的返回值/异常/URI 文本；不接入 ingest/parse/classify/projection/HTTP/UI 任何主流程。
- 不删 F 卡、不删原子卡投影、不停 `l2_projection`、不改 `publish_documents` 现有白名单行为、不删 `hidden`。
- 不写任何业务正则/关键词/行业枚举/映射；模板包是数据，注册表只做结构校验。
- 不新增第三方依赖；不动 `requirements*.txt`。
- 不 push、不部署；只本地 git commit（可 1 个或几个语义清晰的 commit）。
- 不修改、不删除任何原件/解析件/既有 db；测试只用临时目录/临时库。
- 不实现段B（叶子四段式投影、brief/guide 改名接线、停原子卡投影、hidden 下线）和段C（垂类包注册建档）。

---

## 3. 量化验收（CRIS 自检后停手，等饭团黑盒）

1. `git stash` 前后对比：全量 `pytest` 在段A开始前为 **990 passed,1 skipped**；段A结束后为 **(990 + 新增) passed, 1 skipped**，0 failed/error（新增测试数在报告中写明）。
2. `python -c "import app.module_tree, app.module_taxonomy, app.memory.taxonomy"` 成功，且三者的 `MODULES` 与 `TODO_DIRECTORY/UNCLASSIFIED` 完全一致（测试断言）。
3. `load_pack('generic')` 返回 98 叶，计数 12/15/9/9/4/18/19/2/5/5；坏包各类各抛 `ValueError`。
4. 临时库跑绑定 store 全部用例：注册/重复/非法 id/非法 pack/空仓换绑/非空拒绝/锁定拒绝/mark_locked 幂等。
5. 新 URI 函数产出可被 `parse()` 解析且归属正确；既有 URI 解析快照测试零变化；`SAM_FACTS_TREE` 默认关时 `publish_leaves` 抛 disabled。
6. `grep -rn "taxonomy" app --include=*.py` 中真实定义只在兼容 shim；无新增第三方依赖（`pip freeze`/requirements 无变化）。
7. 工作区只新增/改动段A相关文件；`harness-openclaw/` 下既有未跟踪/改动文件**不要碰、不要提交**（那是无关运行产物）；`app/facts_tree/templates/generic/leaves.yaml` 纳入提交。

---

## 4. 极简现场

```bash
cd /home/jakki/projects/startup-ai-manager
git status                      # 先看清楚，忽略 harness-openclaw/ 的既有脏文件
.venv/bin/python -m pytest -q -p no:cacheprovider        # 先复跑基线，应 990 passed,1 skipped
# ……按第 1 节实现……
.venv/bin/python -m pytest -q -p no:cacheprovider        # 全绿
git add <段A相关文件>
git commit -m "feat(facts-tree): scaffold module_tree rename + pluggable template packs + company binding (segment A, no behavior change)"
```

交付报告（写进完工 report）：列出新增/修改文件、新增测试数量与全量最终数字、`available_packs()` 结果、以及“默认开关关闭、主流程零改动”的自证方式（哪些 grep/测试证明）。完成后停手等饭团真机黑盒，不要继续做段B。
