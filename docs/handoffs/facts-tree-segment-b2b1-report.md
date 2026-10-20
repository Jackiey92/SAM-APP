# 事实树 B2b1 回执：文件→叶子编排器

起点：`84be1274a8e094f0579ff4db0034b94c99a3d0cc`。仅本地交付，未 push、未部署；完成后等黑盒，不进入 B2b2/B3。

## 文件清单

- `app/facts_tree/leaf_plan.py`：确定性 `LeafPlanningService`。
- `tests/test_facts_tree_plan.py`：23 项假 worker 用例，全用 tmp_path 测试库与测试文档端口。
- `skills/fact-leaf/SKILL.md`：原技能扩展 plan/leaf，版本 0.2.0，name 与 roles=[] 不变。
- `harness-openclaw/skill-registry/catalog.json`：官方脚本重新生成，仅 fact-leaf version/content_sha256 改变。
- 本回执。

既有 `harness-openclaw/DREAMS.md` 与 memory/ 运行时脏文件未 stage；派单原文 `13-facts-tree-handoff-B2b1.md` 为开工前未跟踪文件，未擅自纳入提交。

## 第 0.1 节实测核对

先按顺序阅读 13、09 v1.2、12；核对 Dossier、LeafPublishService、LeafBuildService、模板绑定、rollup、EmployeeResult、ConsolidationService 实现。
临时数据库 init_db 后 PRAGMA：

```text
facts: id,company_id,entity,attribute,period,value,value_type,unit,source_file,source_page,source_span,valid_from,valid_to,confidence,status,confirm_mode,superseded_by,created_at,module_key
source_files: file_hash,original_name,mime_type,size_bytes,storage_path,origin_zone,status,uploaded_by,uploaded_at
```

module_key 是迁移加列，fact 无 source_loc。复用 `list_verified_facts(company_id=...)` 的当前 verified 过滤，再内存按员工选中叶子的 trunk 过滤，不另写事实查询 SQL。

## plan / leaf 协议与行为

构造 `LeafPlanningService(db_path, memory, *, worker=None, documents)`，构造阶段不打开数据库、不初始化真实员工；documents 深拷贝为任务授权快照。
入口 `plan_and_build(*, company_id, job_id=None)` 第一条操作是 require_enabled；开关默认值不改。

- plan 输入：`action/company_id/template/documents`。template 仅 trunk_code、trunk_directory、leaf_code、title、shape；documents 每项 file_hash、original_name、source_loc、md。仅枚举授予集合，原件名由 B1 已验证来源记录取得，文本全部经 Dossier.elements + Dossier.read，不读原件/objects，不按公司发现文档。
- plan 输出：EmployeeResult(list) 中的 `{leaves:[{trunk,leaf_code}], conflicts?:[...]}`。合法性只检查绑定包存在性与 code/trunk 一致性，顺序去重；非法项记 rejected_plans，不猜、不补。conflicts 只返回，不自动发布冲突件。
- 空 EmployeeResult、空 leaves、无合法叶子：unresolved，不建叶、不汇总、不制造空文件。
- leaf 输入：`action/company_id/target/fragments/facts`；target 为 trunk/leaf_code/title/shape，fragments 同净稿片段协议。facts 仅 fact_id/entity/attribute/period/value/unit/source；source={file_hash,source_page,source_span}，不伪造 fact.source_loc。
- leaf 输出仍是 B2a 的 leaf_code/markdown/source_files/fact_ids/note；先 parse_employee_leaf，再检查声明引用未越出授予文件/输入事实集合，交 LeafBuildService 复用坐标预检、宿主装配与 B1 原子发布。后端不判行业、不选叶子、不写指标规则。
- LeafRejected 记 failed_leaves 并继续；先前成功叶不回滚。WorkerUnavailable/EmployeeTransportError 原样上抛；数据库/文档端口等基础设施异常不伪装业务成功。
- 每个有成功叶（含 no-op）的 trunk 汇总一次，最后整树汇总一次；无成功叶不汇总。rollup 异常原样抛出，异常 planning_result 属性附实际进度与 rollup_error。
- 返回 planned/built/noop/rejected_plans/failed_leaves/rolled_up_trunks/tree_rolled，并附 unresolved/conflicts。built 包含全部发布成功结果；noop 是其中 changed=False 的子集。无跨叶大事务，不双写 fact_leaf_links。

## 假 worker 测试明细

| 用例 | 数量 | 证据 |
|---|---:|---|
| 同枝多叶、真实输入协议、叙述不漏、两次幂等 | 1 | n 型无 fact_ids、无数据明细成功；第二次两叶 noop，revision=2/link=1，文档写次数不增；枝/树各一次 |
| 非法/不一致/自造/错类型 plan、去重与冲突只记录 | 1 | 5 项拒绝、合法叶保留、无冲突件 |
| 坐标/裸数字/跨 trunk/外源/形状/空 leaf 拒收 | 6 | failed_leaves 记录，另一叶与 link 保留，整树仍汇总 |
| 两种传输异常 × plan/leaf | 4 | 原异常上抛，无叶/revision/link 残留 |
| 空 list / 空 leaves / plan 形状错误 | 3 | unresolved，built=[]，零文件 |
| 未绑定 | 1 | 明确拒绝，未调用 worker |
| 关闭态导入、构造与入口 | 1 | DB iterdump 完全相等、文档文件字节相等、writes 不变、worker.calls=[] |
| rollup 失败 | 1 | OSError 上抛，planning_result 精确记录两叶已发布与枝已汇总 |
| 两枝与当前事实过滤 | 1 | 只给对应 trunk 当前 verified，排除 claimed/valid_to/superseded/外公司，保留真实 page/span |
| 同公司但未授权的文件 | 1 | 不能进入 fragments，也不能被引用成功，无净稿泄露 |
| 多文件授予快照 + Dossier.read 路径 | 1 | 调用方清空集合后快照不变；4 个真实片段全部经 read |
| 第二叶传输失败 | 1 | 第一叶与 link 仍有效，异常未吞 |
| 全叶拒收 | 1 | 不汇总，无半成品 |
| 合计 | 23 | `23 passed` |

关闭态证据在 `test_disabled_constructor_import_call_zero_writes`；未改 settings 默认值、依赖文件、document_read/service.py、B1/B2a 实现与旧投影。

## 验证与 catalog

- `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py`：skills=9，rejected_skills=0；技能 rejected.json=[]。
- catalog JSON 归一去掉 fact-leaf version/content_sha256 后与 HEAD 完全相等。
- tool-registry 两文件零 diff；脚本 rejected_tools=1 是基线既有内容，未改变。
- 新增测试：23 passed / 0 failed。
- 新增 + phase1 catalog + phase3 contracts：34 passed / 0 failed。
- `.venv/bin/python -m compileall -q app scripts`：退出 0。
- `git diff --check` 与暂存区检查：退出 0。
- `git diff --exit-code -- harness-openclaw/tool-registry harness-openclaw/config/phase3-roles.json app/document_read/service.py`：退出 0。service.py 无 leaf_plan import；新后端 grep 无纳米/涂膜/前五大。
- 全量最终结果：`1292 passed, 1 skipped in 139.21s (0:02:19)`，0 failed（基线 1269 passed / 1 skipped + 本棒 23 项）。命令 `.venv/bin/python -m pytest -q`；先行全量为 1287 passed / 1 skipped（当时新增仅 18 项），补齐 5 项后已重新跑完整全量。

首次新增测试 2 passed / 16 failed，根因是误把 B1 `_sources` 的 original_name→sha[] 映射当作 sha→name，报 KeyError(file_hash)；已修复，随后新增测试全绿。第一次直接调用 catalog 脚本缺少项目 PYTHONPATH，报 ModuleNotFoundError: app；改用显式 PYTHONPATH=. 与仓库 venv 后成功。未调用真实模型、未新增依赖，测试数据仅临时库，未触碰真实 app.db。

## 未做项

未接 DirectReadService 主流水线；未开默认开关；未调真模型；未做预算/断点续跑/多文件合并调度；未自动发布冲突件；未改角色挂载、插件工具、建档 UI 或显隐 UI；未执行 B2b2/B3、黑盒/真机/部署/push。等待饭团黑盒验收。
