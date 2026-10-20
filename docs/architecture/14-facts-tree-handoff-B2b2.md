# 事实树 B2b2 交接详档：编排器接入 document_read 主流水线（预算 + 断点 + 真模型通道）

给 CRIS。本棒在 B2b1（`19d4395`，已黑盒：1292 passed）之上做 **B2b2**：把 B2b1 的文件→叶子编排器接进 DirectReadService 主流水线，受同一预算/断点机制约束，走同一受控模型通道。权威仍是 `09` v1.2（§8 文件驱动）、本档与 `13`（B2b1）。

## 0. 铁律（不变）
- 只本地 commit，不 push、不部署；不 stage `harness-openclaw/` 既有无关脏文件；零新第三方依赖；后端零业务规则。
- **默认开关关闭，关闭态行为逐字节不变**：`DirectReadService.run` 在 `SAM_FACTS_TREE=False` 时，limits 仍只校验 classify/attribute/extract、不出现任何叶子阶段、state 形状不变、不产生 plan/leaf 调用。必须有关闭态回归（建议对 service 层做关闭态快照/调用计数断言：叶子 worker 0 次、memory_kv state 无叶子键）。
- 只测试库；不删原件/facts。真模型鸿通晟端到端是 **B3**，本棒用注入 fake model/worker 覆盖接线、预算、断点，不要求真跑模型。
- 遇口径冲突停手列点。

## 0.1 以现有代码为准（已核对）
- `app/document_read/service.py:DirectReadService`
  - `__init__(self, db_path, *, config, dossier, memory, model=None)`；`self.model` 默认 `EmployeeModelTool(config)`；`self.skill` 是 file-processor；`self.facts=ConsolidationService`。
  - `_call(self, action, payload, state, limits)`：`phase = action if action in ('classify','attribute') else 'extract'`；先按 `state['calls'][phase] >= limits[phase]` 判 `BudgetStopped(phase)`，计数 +1、置 `state['active_action']=action`、charge 前落 state，再 `self.model.execute(...)`。
  - `run(*, company_id, job_id, limits)`：开头强校验 company_id 与 limits 三个键（classify/attribute/extract，非负 int）；校验 dossier 保真指纹；state 存 `sam://kv/rt/md-direct/<job_id>`，可断点续跑；新 state 含 calls/classification/ranges/complete/accepted/fact_ids/unresolved/brief_done/guide_done。
  - 流程：classify → attribute（一次结构纠正）→ while not complete: extract（增量 remaining_source_locs，BEGIN IMMEDIATE 内 consolidate_candidates，更新 fact_ids/l2_committed）→ **`L2ProjectionService(self.db_path,self.memory).publish(...)`（约 173 行）** → 内部 `Reader`（自定义 `.run(*,skill,text,**kwargs)`，把 brief/guide 转成 `service._call`）→ L1BriefService.rebuild → L0GuideService.rebuild → `state['done']=True`；`except BudgetStopped: status='partial'`；其它异常分类落 partial/上抛；最后 `_finish`。
  - **唯一调用方**：`scripts/dev/run_m5b_real.py`（约 93/140/143/151 行）。
- B2b1：`app/facts_tree/leaf_plan.py:LeafPlanningService(db_path, memory, *, worker=None, documents)`，`plan_and_build(*, company_id, job_id=None)`；构造惰性、内部 `require_enabled()`；documents 是 `{file_hash: document}`（document 即 dossier.document，含 markdown/document_map）；worker 为 `.run(*, skill, text, company_id, thread_id=None, bounded_model=False, budget=None)` 的对象（见 `EmployeeRunner.run`）。
- `EmployeeRunner.run` 支持 `budget`/`bounded_model`，bounded 时走 `EmployeeModelTool`。本棒主流水线不直接 new 一个非 bounded EmployeeRunner，而是注入一个走 `service._call` 的 worker 适配器（与 `Reader` 同形态），从而叶子调用也被计数、charge-before-transport、可 BudgetStopped。
- `BudgetStopped(RuntimeError)` 在 service.py 顶部。

## 1. 目标
门开时，在**单文件** DirectReadService 完成事实抽取与 L2 publish 之后、L1 brief 之前，新增"叶子阶段"：
1. 用本文件授予集 `{dossier.file_hash: dossier.document}` 构造 `LeafPlanningService`，worker 注入为新的 `_LeafWorker` 适配器（plan/leaf 动作都走 `self._call`，受预算约束）。
2. 叶子阶段**可断点续跑、按叶不重复计费**：plan 只在首次调用并把结果固化进 state；续跑复用，不重复花 plan；已成功构建的叶不重复调 leaf；全部计划叶建完才做 rollup（rollup 在编排器内，幂等）。
3. 预算：门开时 limits 增加两个非负整数相 `plan`、`leaf`（plan 动作记 plan 相，每个 leaf 动作记 leaf 相）。预算用尽 → `BudgetStopped` → 现有 partial 路径，state 保留进度，提高预算后续跑。
4. 关闭态完全不进入本段（见铁律）。

非目标：不多文件合并调度（仍一文件一 run）；不改 L1/L0 旧产物语义；不做开关默认打开/建档 UI；不跑真模型端到端（B3）。

## 2. 施工要求

### 2.1 编排器可恢复化（改 `leaf_plan.py`，保持 B2b1 测试全绿）
把 `plan_and_build` 拆成可组合的三步，`plan_and_build` 仍是这三步的便利封装（B2b1 行为/返回不变）：
- `plan(company_id, job_id=None) -> (planned_codes, conflicts/unresolved 信息)`：require_enabled、绑定/包校验、生成 fragments、调 worker plan、后端合法性校验（沿用 B2b1：trunk/leaf_code 一致、去重、rejected_plans）。返回合法 planned 列表与诊断；空 plan 返回空（unresolved）。
- `build_one(company_id, leaf_code, job_id=None) -> entry|None`：按 B2b1 单叶逻辑取本 trunk verified facts、调一次 worker leaf、证据集/fact 引用校验、`LeafBuildService.build_leaf`；`LeafRejected` 计入 failed（由驱动聚合），返回 built entry（含 changed/noop）。
- `finalize(company_id, affected_trunks) -> rollup 结果`：对受影响 trunk rollup_trunk + 一次 rollup_tree（沿用 B2b1 异常处理：rollup 失败把部分进度挂异常上抛）。
- `plan_and_build = plan → 逐个 build_one → finalize`，返回结构保持 B2b1 那套键（planned/built/noop/rejected_plans/failed_leaves/rolled_up_trunks/tree_rolled/unresolved/conflicts）。
- 绑定/包/存储/ resolver 的初始化不要每步重复构造：允许内部一个 `_prepare()` 惰性建好 store/resolver/builder/fragments，多次调用复用；`close()` 仍在 finally。

### 2.2 service.py 接线（仅门开）
- run() 的 limits 校验：保持关闭态只认老三样；**门开时**额外要求 `plan`、`leaf` 为非负 int（用 settings.SAM_FACTS_TREE 分支，不要让关闭态因缺新键报错）。
- `_call` 的 phase 映射扩展：`action in ('classify','attribute','plan')` 时 phase=action，`action=='leaf'` 时 phase='leaf'，其余 'extract'。新 state 的 `calls` 初值在门开时带 plan/leaf 两个 0（关闭态 calls 形状不变——注意续跑老 state：门开且老 state 无该键时用 setdefault 补，不破坏关闭态）。
- 在 L2 publish 成功之后、L1 之前，`if settings.SAM_FACTS_TREE:` 调一个新内部方法 `_run_leaves(state, company_id, job_id, limits)`：
  - 构造 documents={self.dossier.file_hash: self.dossier.document} 的 LeafPlanningService（db_path/memory 同源），worker 传 `_LeafWorker(self, state, limits)`。
  - `_LeafWorker.run(*, skill, text, company_id, **kw)`：解析 text JSON 的 action；action='plan' → `resp=self._call('plan', payload, state, limits)`，返回 `EmployeeResult([resp])`（B2b1 已兼容"plan 返回 list 内含 {'leaves':...}"）；action='leaf' → `resp=self._call('leaf', payload, state, limits)`，返回 `EmployeeResult([resp])`。不得在此做业务判断，纯转发+计数。
  - 断点：
    - 首次：`planned, diag = planner.plan(...)`；把 `state['leaves_planned']=planned`、诊断（rejected_plans/conflicts/unresolved）存 state 并 `_save`。
    - 续跑：若 state 已有 `leaves_planned` 直接用，**不再调 plan**。
    - 逐叶：维护 `state['leaves_built']`（成功叶 code 列表，含 changed/noop 结果摘要）与 `state['leaves_failed']`；对 planned 中尚未 built/failed-terminal 的 code 调 `build_one`；`LeafRejected`（编排器内部已转 failed）记 failed 并继续（单片坏不拖批，沿用 B2b1）；传输类异常（含 `_call` 的 BudgetStopped/EmployeeTransportError）**不吞**：BudgetStopped 让其抛出落 partial（已建叶保留、state 已落盘），其它传输异常走现有 stage_failure 分类。
    - 所有 planned 叶处理完（built 或 failed 都算处理完）后调 `planner.finalize(...)`，成功置 `state['leaves_done']=True` 并 _save。
  - 叶子阶段失败/partial 不得阻断已经提交的 facts 与 L2（顺序保证 facts 先落定）；但 leaves_done 未完成时 brief/guide 是否继续：**本棒规定叶子未 done（partial）时不进入 brief/guide，直接随 BudgetStopped/异常落 partial 续跑**（续跑会先补完叶子，再 brief），避免旧 L1 在叶子缺失下生成。leaves_done=True 后沿用原 brief/guide。
- 关闭态：整段不执行，state 不含任何 leaves_* 键，grep 得到的接线都在 `if settings.SAM_FACTS_TREE:` 门内。

### 2.3 预算语义
- plan 调用 1 次扣 1 个 plan 相；每片 leaf 调用扣 1 个 leaf 相；续跑不补扣已完成动作。
- limits['leaf']=0 且门开：有计划叶时首次进 leaf 即 BudgetStopped('leaf')，partial，planned 已固化（验证：提高 leaf 预算后续跑不重新 plan、不重建已建叶）。
- rollup 不调模型、不扣预算。

## 3. 测试（tmp_path，注入 fake model/worker，不连真模型）
新增/扩展 service 层测试，**直接复用** `tests/test_document_read.py`（291 行）现成夹具，不要另起炉灶：
- `task` fixture（约 26 行）建临时 db + `document={'markdown':...,'document_map':{'elements':[{'source_loc':'table','start_line':1,'end_line':3}]},'assets':[]}` + `Dossier(...)`，并在约 39 行设 `dossier.quality_fingerprint=dossier.fingerprint`（不过保真门）。
- `Model`（约 48 行）：`__init__(responses=None)`，`self.actions=[]` 记录动作、`self.responses=iter(...)` 按序吐响应，`execute(**kwargs)` 返回值（注意它返回的是 dict 还是 JSON 串，以现有实现为准，新动作响应保持同形态）。
- `service(task, model)`（约 59 行）用 `SimpleNamespace(skill_root=ROOT/'skills')` 构造 DirectReadService。
- 预算/续跑断言范式见 `test_budget_preserves_and_resume_no_duplicate`（约 87 行，用 `model.actions` 列表精确断言调用序列与不重复）、`test_zero_budget_never_calls`（97 行）。
- 门开测试用 `monkeypatch.setenv`/直接 set `app.settings.SAM_FACTS_TREE=True`（以现有 facts_tree 测试的开门方式为准，保持一致），并确保该公司已有模板绑定（B2a/B2b1 测试里有绑定写法，照搬）。
- 叶子 plan/leaf 的 fake 响应里，坐标必须用夹具 document_map 真实存在的 source_loc（如 'table'），否则会被严格坐标预检拒收。
具体用例：
1. 门开全绿：fake model 依次响应 classify/attribute/extract/plan/leaf/brief/guide，跑通后 leaves_done=True、叶文件落 sam://facts、rollup 产物存在、parse_jobs done。
2. 预算 partial→续跑：leaf 预算给 1（计划 2 叶）→ partial 且状态保留；提高后续跑→不重复 plan 调用（用计数断言 plan 总次数=1）、已建叶不重复 leaf 调用、最终 done 且 2 叶齐全。
3. plan 预算=0：plan 阶段即 BudgetStopped，partial，无叶落盘，续跑补预算成功。
4. 关闭态回归：SAM_FACTS_TREE=False 跑同一输入，全程无 plan/leaf 调用计数、state 无 leaves_* 键、产物与旧链路一致（不要求文件字节快照，但要求叶子 URI 零写入、facts/L2 行为不变）。
5. 单片坏隔离：某 leaf 的 fake 响应坐标非法→该叶 failed、另一叶成功、rollup 仅含受影响 trunk、leaves_done=True（failed 不阻断其它叶与收尾）。
6. 传输异常：leaf 动作模型抛 EmployeeTransportError→落 stage_failure partial，已提交 facts 不回滚、已建叶保留，续跑可恢复。
7. 断点证据一致：续跑前后 dossier 指纹/scope 校验仍生效（沿用现有 resume evidence changed 抛错）。
8. 门开但 limits 缺 plan/leaf 键：明确报错（host 必须显式给预算）；关闭态缺这两键不报错。

## 4. 验收硬标准
- 新增测试全绿；全量在 **1292 passed / 1 skipped** 上只增不减、0 failed。
- compileall、git diff --check；零新依赖；后端无业务词/关键词/枚举。
- 关闭态零叶子行为（专门测试 + 人工 grep 接线全在门内）；run_m5b_real.py 默认门关路径不受影响（可不改脚本，或仅在门开分支补预算，不得改默认行为）。
- 回执 `docs/handoffs/facts-tree-segment-b2b2-report.md`：分步 API、_LeafWorker、预算相、断点 state 键、partial/续跑证据、关闭态证据、未做项（真模型端到端留 B3）。
- commit 遇只读沙箱则保留暂存并报告，饭团侧提交。停手等黑盒，不进 B3。
