# 事实树 U2：单文件逻辑员工并行及可靠续跑

## 状态与施工口径

按AGENTS、22号单、U1回执及本线程U2要求施工；只U2，未接网页上传/待办UI/总结模型。HEAD a1dfe7b，无本棒commit。饭团提供U1小型真实模型验收通过：4窗口、20请求、3叶、冲突待办且旧值保留；这是外部验收背景，非本棒真机结果。U1此前1480/1为事实检查点修复前历史，不能冒充当前全量。

## 文件及最小结构

- 新增 app/document_read/execution.py：JobLease / LeaseLost / JobBusy，run_files有界多文件执行入口；复用标准库ThreadPoolExecutor（ParseJobManager也用该设施），没有新依赖。
- 修改 app/document_read/service.py：门开run独占reader实例并领取持久job租约，所有_save（含计费、事实事务检查点、_finish）在同写事务检查代次/有效期；释放仅匹配自己owner/generation。关闭态与run_legacy不领租约。U1副本事实检查点提交顺序保留。
- 修改 app/facts_tree/md_reader.py：发布传入租约fence；BaseChanged丢弃旧draft（含冲突判断），有界重读最新叶/重新调员工比较，不再只抛错等待手动重跑。竞争累计次数持久记录，达到每轮上限partial且游标停留；恢复仍受持久模型总账限制。
- 修改 app/facts_tree/md_publish.py：submit原事务内写前/提交前检查fence，失效整批回滚；普通新增与待办原子性不改。current增加短BEGIN IMMEDIATE阅读保留锁，避免分别读取修订/侧表/provider投影时跨越另一个提交而误报SHA不符；已有写事务直接复用，绝不在锁内调用员工模型。R join及S provider源码不改。
- 新增 tests/test_document_read_parallel.py：真实provider/隔离SQLite，离线替身员工，多进程争用/恢复及执行入口测试。
- 本报告。

## schema / 升级兼容

仅门开显式run时CREATE TABLE IF NOT EXISTS md_job_leases：job_id主键、company_id、owner随机宿主标识、generation递增、expires_at秒级绝对时间。没有全局schema版本/迁移/触发器变动，不扫描或转写真实数据。lease释放不删除行，保留代次；已过期才允许新宿主领取。SQL写前BEGIN IMMEDIATE+条件匹配，与后续检查点/计费/发布在同一锁/事务内，无“先验租约后独立写”的竞态。

默认lease_seconds=360，可配置240..900；单次模型timeout仍180s。每次保存/计费/发布边界检查并续约，模型期间不持SQL锁、不依靠进程内公司锁。宿主崩溃无释放时等剩余TTL；到期旧宿主不得续约/save/计费/发布，LeaseLost明确抛出，不伪装done。任意长暂停或超过TTL的存储阶段可以失去租约，这是安全失败，不自动保活。

## 调用入口 / 预算 / 显式续期

单文件：
```python
reader.run(company_id=company, job_id=job,
    limits=dict(classify=C, attribute=A, extract=E, plan=P, leaf=L),
    lease_seconds=360, compare_retries=3,
    renew_deadline_seconds=None)
```

多文件：
```python
from app.document_read.execution import run_files
results = run_files([
    dict(reader_factory=owning_reader_context_factory,
         company_id=company, job_id=job_a, limits=limits_a),
    dict(reader_factory=another_reader_context_factory,
         company_id=company, job_id=job_b, limits=limits_b),
], workers=2)
```
factory必须在执行线程创建该文件独立reader/provider，可返回上下文管理器（退出关闭自己的provider），也可返回调用方管理生命周期的fresh reader。workers=1..8，每批显式tasks<=256，结果按输入顺序，单job异常作为异常对象返回，调用方必须检查，不能当成功。共享一个reader同时执行明确JobBusy。

同job跨进程由SQLite租约独占，不同job同公司可并行模型工作；SQL短事务串行。没有复用ParseJobManager生命周期（其启动会处理旧上传job状态），没有改webapp::_parse_job_pipeline；网页上传仍旧链，U3才核验接线。

预算是各phase持久calls的生命周期累计上限，恢复不清零；每次传输前原子保存账单。不承诺网络exactly-once。compare_retries每次run每分配1..8（默认3），是本轮争用尝试上限；累计base_changes持久，所有重比对仍消耗总leaf预算。耗尽不推进窗口，续跑继续最新读/比较。

默认deadline_at首次设为30分钟且保存，过期原样partial，不隐式刷新；只有明确传renew_deadline_seconds=1..1800才以当前时间建立新截止并增加deadline_renewals，保留calls/已提交回执/窗口，不自动无限续命。

MDWindows保持U1逐element/大表切片规则：W=len(MDWindows(dossier).windows)，目录元素不等于必然窗口数（大表可拆多窗）。114个短映射元素若一元素一窗则W=114，正常无纠错/争用五相各1次，共570次；不在本棒读取或真跑原件验证该估计。正常每窗5次；提取重试与同叶竞争会增加extract/leaf次数，上界由明确五phase总limits限定。需调用方给覆盖W的预算或接受partial；没有偷偷扩classify=2/plan=2，不机械合窗、不放松多坐标授予。

## 新测试对应（离线，不是真模型）

- test_two_process_jobs_real_provider_same_leaf_recompare_and_slow_model（三参数）：两真实进程各自开provider，同SQLite同公司不同文件；首稿均base0，第二员工慢模型等第一完成，随后被CAS拒并看到version1；新增保留双方或冲突双方进待办且原叶保留；不同叶慢模型不阻塞第一员工。
- test_job_lease_claim_expiry_generation_fences_checkpoint_billing_and_publish：同job争领、到期接管、旧代checkpoint/计费/发布拒绝、旧release不影响新代。
- test_same_job_process_claim_crash_takeover_budget_and_old_host_fenced（两参数）：传输计费后进程崩溃或旧进程仍存活；模拟租约到期，新宿主续跑，旧宿主再回来LeaseLost，已提交state不被覆盖，calls累计不清零。
- test_contention_exhaustion_is_partial_then_reread_resumes_with_bill：有界竞争耗尽partial，过期稿删除，恢复重新比较且模型账单/已提取事实步骤不重复。
- test_leased_submit_lost_receipt_replay_no_model_or_history_duplication：真实提交后回执抛错，新代重放成功操作，无模型/历史/通知/操作重复。
- test_deadline_explicit_renewal_preserves_budget_and_finished_receipts：截止过期不隐式续命，显式续期后完成，总账保留，done重放无新操作。
- test_interrupted_transport_budget_exhaustion_never_resets_on_new_generation：不确定传输已计费；新代不能绕过已耗尽限额，显式提高总限额才完成。
- test_bounded_parallel_entry_real_readers：真实多文件入口两线程/独立provider，有界执行，上下文清理。
- test_parallel_entry_bounds（三参数）：并发配置边界拒绝。

共14个新增参数化实例（待正常WSL收集确认）。既有U1四类事实检查点故障39例及关闭态/R/S相关回归也需复跑，本棒未删任何断言。

## 实际检查 / 限制

首次相关回归命令 `.venv/bin/python -m pytest -q tests/test_facts_tree_mapped_md.py` 在收集前exit 4，原始完整错误：
```
ImportError while loading conftest 'tests/conftest.py'.
tests/conftest.py:34: with socket.socket() as listener
/usr/lib/python3.14/socket.py:236: _socket.socket.__init__(...)
PermissionError: [Errno 1] Operation not permitted
```
按纪律没有重试/改conftest/绕过隔离；新增多进程专项尚未执行，0个本棒通过数字可报告，不能以U1历史通过替代。饭团正常WSL待跑 tests/test_document_read_parallel.py、test_facts_tree_mapped_md.py 及关闭态/R/S回归。未跑全量，未调真模型。

compileall/diff-check最后真实结果另列。仅门开宿主执行设施改变；不改员工语义规则/技能/catalog/tool-registry/原数字坐标护栏，不迁移真实数据、不读取凭证。不碰既有harness运行脏文件；无stage/commit/push/deploy。U3上传/待办可见/完整原件/跨多员工压力验收未做；停手等饭团黑盒。

### 最后静态检查

- `.venv/bin/python -m compileall -q app tests scripts`：exit 0。
- `git diff --check`：exit 0。
- protected diff（app/memory、app/db、test_document_read.py、document_read_closed_b2b1.json）为空；U2未修改既有harness运行文件/技能/catalog/tool-registry。git status仍包含U1已授权改动及原运行脏文件，不stage。
- 全量本棒未执行；新增14实例及U1/关闭态/R/S回归均待正常WSL。当前HEAD a1dfe7b，无提交。交付后停手。

## 多进程会合夹具返工及实际复跑

饭团正常WSL前轮两文件专项：3 failed / 50 passed，8.53s（原日志 `/tmp/sam-u2-targeted.log`）。三例在Barrier初始化即失败：Python3.14 Barrier→BufferWrapper→Arena→tempfile.mkstemp写/dev/shm/pym-*，isolation_guard正确拒绝隔离根外写入；未到业务断言，非业务代码缺陷。

本次仅改 tests/test_document_read_parallel.py 与本报告。以FirstDraftMeeting包装两个独立Event：各进程到首个leaf比较时仅设置自己的到达事件，并限时等对方事件；双方首稿均已读current后才能继续。仍为两个真实进程/独立真实provider，第二员工继续等待第一提交，原CAS重读、冲突双方、正常新增双方、不同叶慢模型无阻塞等全部断言保留。没有Barrier/Arena文件，没有改conftest/isolation_guard/白名单，没有改业务代码或降为线程。

本轮沙箱实际正常运行：
- `.venv/bin/python -m pytest -q tests/test_document_read_parallel.py -k two_process_jobs`：3 passed / 11 deselected，1.34s。
- `.venv/bin/python -m pytest -q tests/test_document_read_parallel.py tests/test_facts_tree_mapped_md.py`：53 passed，9.30s，0 failed。
- `.venv/bin/python -m compileall -q app tests scripts`：exit 0。
- `git diff --check`：exit 0。

前节“尚未执行”是此前受限运行记录，本节更新专项终态，不冒称全量或真实双员工模型黑盒通过。本轮未全量长跑、未调真模型、未stage/commit/push/deploy，未碰harness运行脏文件。停在U2等待饭团黑盒，不进入U3。
