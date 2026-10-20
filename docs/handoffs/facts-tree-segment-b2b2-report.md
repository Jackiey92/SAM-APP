# 事实树 B2b2 回执：门控主流水线、预算与断点

起点：`19d439578a62bc6bc2e0cc120498b31a8a916c4f`（1292 passed / 1 skipped）。仅本地交付，未 push、未部署；停手等黑盒，不进入 B3。

## 文件清单

- `app/facts_tree/leaf_plan.py`：拆分 plan/build_one/finalize，保留 plan_and_build 的原返回契约。
- `app/document_read/service.py`：门控叶子阶段、_LeafWorker、plan/leaf 预算相与逐叶断点。
- `tests/test_document_read_leaves.py`：25 项新增测试，复用旧 task/Model/service，门开仅用 task 的独立 dossier 变体。
- `tests/fixtures/document_read_closed_b2b1.json`：由 19d4395 原 service 捕获的关闭态字节快照（仅冻结事实写入时钟）。
- 本回执。

默认开关、B1/B2a 产品护栏、原 `tests/test_document_read.py` 裸 table 夹具、技能/catalog/工具清单、依赖、run_m5b_real.py 均未修改。harness-openclaw/ 既有运行时脏文件不 stage；开工前未跟踪的 13/14 派单原文不擅自纳入提交。

## 0.1 核对与坐标裁决

已读取 14 唯一施工口径、13 与 09 v1.2 背景，并核对 service._call/run、EmployeeModelTool、EmployeeResult、LeafPlanningService、Dossier 与引用护栏。
原 _call 预扣预算、active_action 落库后才 model.execute；brief/guide 共用 extract 相；run 的事实正本提交与 L2 publish 均在叶子阶段之前。

此前发现老 task 坐标 table 与 B1 脚注冒号护栏冲突，已停手报告；本次按用户明确裁决恢复：**不改 B1/B2a、不改原关闭态夹具**，open_task 深拷贝 document，将唯一 element.source_loc 改为 `docx:body/16/table`，重新构造 Dossier 并设置匹配的保真指纹。fragments、事实源坐标、员工叶子脚注全部使用这个同一真实字符串；B1 格式检查与 Dossier.read 精确命中闭环。

## 分步 API

- `plan(company_id, job_id=None) -> (planned, diag)`：diag 含 rejected_plans/conflicts/unresolved；绑定包与 plan 合法性、去重和空 plan 处理沿用 B2b1。员工仍唯一决定叶子。
- `build_one(company_id, leaf_code, job_id=None) -> entry | None`：当前 verified facts 仍复用 list_verified_facts，再内存按该 trunk 过滤；LeafRejected 追加到实例 failed_leaves 并返回 None；业务驱动读本次最后一项拒收，不把预算/传输异常算作终止拒收。
- `finalize(company_id, affected_trunks)`：树枝顺序去重，各一次 rollup_trunk；有成功树枝时一次 rollup_tree。返回 rolled_up_trunks/tree_rolled；异常附 planning_result（实际汇总进度及 rollup_error）并上抛。
- `_prepare` 在首次调用惰性初始化、复用 store/resolver/builder/fragments；宿主公司 scope 不可切换。初始化异常立即 close，驱动始终 finally close。
- `plan_and_build` 为三步便利封装，原 planned/built/noop/rejected_plans/failed_leaves/rolled_up_trunks/tree_rolled/unresolved/conflicts 契约与 B2b1 23 项测试保留；noop 仍是 built 中 changed=False 的子集。无新增业务判断/指标枚举/归类规则。

## _LeafWorker 与预算

`_LeafWorker` 只解析 JSON action、验证技能/宿主 scope，然后转发 `service._call('plan'|'leaf', payload, state, limits)`，返回 EmployeeResult([response])。不 new 非 bounded EmployeeRunner、不另建模型通道、不加隐藏重试。

- 模型仍为原 self.model（默认 EmployeeModelTool），timeout 仍为 180；门开的 plan/leaf 从原 skills/fact-leaf/SKILL.md 读取技能文本，其余动作继续原 file-processor 技能文本。
- 门关只校验 classify/attribute/extract；门开额外强制 plan/leaf 为非负 int，bool/float/缺键拒绝。
- phase 映射：classify、attribute、plan、leaf 各自计数，其余仍 extract；每次 _call 在 transport 前递增并保存。
- 新 state 的 calls 来自显式预算；门开恢复旧 partial state 时 setdefault 补 plan/leaf=0。门关不补新键、不进叶子阶段。
- 预算是累计调用上限，不是每次 run 重置额度；提高上限续跑。失败传输也扣费，rollup 不调模型、不扣额度。

## 主流水线断点 state

叶子阶段仅在 `if settings.SAM_FACTS_TREE:` 下进入，位置为事实正本提交 + L2 publish 成功之后、L1 brief 之前。

| 键 | 用途 |
|---|---|
| leaves_planned | 固化合法 code 列表；存在即不重复 plan，包括空计划 |
| leaves_diagnostics | 原 plan 的 rejected_plans/conflicts/unresolved |
| leaves_built | 成功 entry 列表，含 leaf_code/uri/version/changed/updated_at（含 no-op） |
| leaves_failed | 业务终止拒收 leaf_code/reason；恢复不再重试这些叶 |
| leaves_rollup | 汇总结果；异常时记录部分进度和 rollup_error |
| leaves_done | 所有计划叶 built 或 terminal failed，并 finalize 成功后才置 True |

计划、每片终止结果都立即 _save；恢复从 SQLite 读取，跳过已完成动作。BudgetStopped 上抛到原 partial 路径；EmployeeTransportError/WorkerUnavailable 走 stage_failure partial，保留已提交 facts/L2 与先前成功叶。非传输基础设施异常保持原异常上抛，_finish 保存 partial；汇总失败恢复只补机械汇总，不重建叶。leaves_done 未成功时不能进入 brief/guide；done 后续跑只补未完成摘要。

## 假模型验证证据

专项命令：

```text
.venv/bin/python -m pytest -q tests/test_document_read_leaves.py tests/test_facts_tree_plan.py tests/test_document_read.py
77 passed in 8.85s
```

25 项本棒测试 + B2b1 原 23 项 + 原 document_read 29 项，0 skipped / 0 failed。假 Model 在每次 execute 内查询已落 SQLite state，**精确断言调用次数等于此前该相动作数 + 1**，验证 charge-before-transport，而非事后只看计数。

| 新用例 | 数量 | 实测要点 |
|---|---:|---|
| 门开单叶全程与幂等 done | 1 | actions=classify/attribute/extract/plan/leaf/brief/guide；技能文本/超时/授予集合/真实坐标检查；叶/树枝/整树产物存在；parse_jobs done |
| 2 叶、leaf 上限 1→2 | 1 | partial actions=classify/attribute/extract/plan/leaf；重建 service 后只补第二叶、brief、guide，plan=1、targets 恰为两 code，首叶 revision 不变 |
| plan=0 / leaf=0→提高 | 2 | 零额度不调用对应相；leaf=0 时计划已固化；恢复动作不重复 |
| 坏叶隔离与跨枝收尾 | 1 | 坐标不存在记 failed，另一 hr 叶成功，rollup 仅 hr，仍 done |
| terminal failed 后预算续跑 | 1 | 不重复坏叶、不重复 plan，只补未处理叶 |
| 第二叶传输失败→恢复 | 1 | 先前 facts/首叶/link 保留，leaf 已扣 2 次；额度提高到 3 只重试第二叶，actions 精确断言 |
| fingerprint / scope 改变 | 2 | 续跑前拒绝，未新增模型调用 |
| plan/leaf 缺键、负数、bool、float | 8 | 明确输入错误、模型零调用 |
| 专门关闭态字节回归 | 1 | 下文详述 |
| 老三相 partial 门开恢复 | 1 | setdefault 补新计数，不重复已完成 extract |
| 叶 done、brief 预算停→恢复 | 1 | 不重跑 plan/leaf，不改 revision，仅补 brief/guide |
| 空 plan / 非法 plan | 2 | 固化空结果与 unresolved，零 leaf 调用、零空叶 |
| plan 传输错误/WorkerUnavailable | 2 | 预扣 plan，partial 无伪造计划，增加上限后恢复 |
| rollup 失败→恢复 | 1 | 记录部分汇总并抛 OSError，不进摘要；恢复无模型重建叶 |
| 合计 | 25 | 全绿 |

## 关闭态零变化证据

`test_closed_state_and_old_outputs_no_leaf_stage` 保留旧 task 的裸 table，预算仅老三键：

- 把 LeafPlanningService 构造与 _LeafWorker.run 替换成失败哨兵，运行仍成功，说明叶子构造/调用零次。
- actions 精确为 classify/attribute/extract/brief/guide；calls 精确老三相，state 无任何 leaves_* 键。
- 19d4395 原 service 在临时 task 上运行、仅冻结 consolidation 时钟，捕获原 memory_kv state JSON 和全部 5 份文档；快照纳入 fixture。新关闭链路与此快照 **state JSON 和每份文档字符串逐字节相等**，不是只比计数。
- facts=1、l2_committed=1、parse_jobs done；leaf_revisions=0、无 leaves/ 或树汇总新 URI。
- 再次 done 调用，state JSON 字节与全部文档字节不变。

## 全量与静态检查

- 全量 `.venv/bin/python -m pytest -q`：`1317 passed, 1 skipped in 151.27s (0:02:31)`，0 failed；基线 1292 + 新增 25，原测试数不减。
- `.venv/bin/python -m compileall -q app scripts`：退出 0。
- `git diff --check` 与 `git diff --cached --check`：均退出 0。
- B1/B2a 护栏、settings、旧测试、技能/catalog/tool-registry、依赖与 run_m5b_real.py：零 diff。
- 新后端 grep 无纳米/涂膜/前五大；无业务归类词表、指标映射或新增依赖。所有模型执行均为假模型；没有网络/真实模型端到端。

## 未做项

真模型鸿通晟端到端留 B3；未做多文件合并调度、建档 UI、开关默认打开、工具/角色变更、push、部署或黑盒真机。跨叶不设大事务；进程在单叶发布成功但 runtime checkpoint 尚未保存的极窄窗口被强杀时，B1 发布依旧幂等，但无新做跨库原子计费/发布协议（本棒按指定逐叶保存机制实现）。

本回执所述是本棒实现与假模型证据，不替代黑盒与真机放行。完成后停手等饭团验收。
