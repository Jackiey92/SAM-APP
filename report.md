# M5-B 真机打回修复（2026-10-13，本地交付，待饭团黑盒）

## 根因与原始报文证据

- 读过饭团两份原始日志 `/tmp/m5b_real_run_164315.log`、`/tmp/m5b_full_164640.log`。原请求允许输出 32768 tokens，同时要求整份事实一次完整交付，子进程只有 180 秒。诊断原配置的实际请求/响应留在 `/tmp/sam-m5b-real-27bg_txw/model-trace/`（同名 request/response/metrics 三件套，0600，无 Authorization/秘钥）；运行日志 `.runtime/m5b-diagnostic-original.log`。extract 实测 139.3045 秒，输入 16324 / 输出 11051 tokens，请求 47613 bytes、响应 35258 bytes，86 个候选，非流式、finish_reason=stop。同份长输入 classify/attribute 仅 5.1101/3.4033 秒。这次没有重现 180 秒超时，证据支持“超长输出逼近时限”，不能据此断言饭团那次端点内部耗时或首 token 延迟；非流式没有 TTFT 证据。超时并非通过无限增大 timeout 掩盖。
- 零事实 done：此前直接信任员工 complete=true，没有“至少一条已核验事实”终态约束；原候选中 JSON 数字与过严字符串要求冲突，以及逐字 entity 匹配拒绝范围内不同实体，全部拒绝仍可结束。饭团低预算旧根 `/tmp/sam-m5b-real-60p90uh_` 有 84 条 unresolved（62 条契约类型、22 条范围实体匹配），0 入库却 done。

## 修改与护栏

1. 同一个文件处理员仍负责所有动作；每次 extract 最多 20 条、默认输出 8192 tokens。员工自己维护 remaining_source_locs 队列，后端仅按其合法坐标取原文、校验队列不回开已完成范围。already_delivered 为已验证事实的紧凑卡片；不加业务分段/公式/词表/答案映射。原有旧碎块链未切换或删除。
2. transport/TimeoutExpired/坏 JSON 变为可恢复 partial，调用前计费，已提交事实与工作进度保留，同 job 增加累计预算可续跑；超时异常不落完整命令/提示词。临时候选 checkpoint 用 deepcopy，事务失败不把未提交事实伪装成已交付。零已核验事实不能 complete/done、不能 l2_committed=1。预算首轮有事实则事务提交，下一次请求前 BudgetStopped -> partial。
3. 七个必填事实字段不变。JSON 数字标量仅序列化（不换算或猜期间）；缺可选 value_type 保守使用 string，未知期间按现有 verifier 记 unspecified；有显式 attribution_index 时核对范围坐标，不由后台别名化实体。quote 必须在该坐标和归属范围交集的连续原文中通过现有核验，不能借同表其他行“证明”。拒绝记录进入 unresolved/todos，不整份清零。
4. 简报/导览仍由同员工生成，复用未放宽的数字/出处护栏；单个坏条目隔离，有合法兄弟继续投影。全部条目结构/引用不合格时只允许一次额外、真实计费的格式纠正，无主机改写答案；仍不合格则 partial。归属范围越界也只允许一次计费格式纠正，不裁剪模型区间。所有纠正均计入三阶段预算。
5. 技能补充财务表头逐年、金额精度、编号/日期 string、正文冲突保留、逐字引句、队列纪律、简报导览引用契约。新技能显式加入测试的 8 技能白名单，只追加 file-processor，断言结构/其他 catalog/rejected 测试不变。目录通过 `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py` 正常生成：8 skills / 0 rejected skills；rejected.json 未手改。
6. 真机脚本强制新临时根、bind:0 保留随机本地端口，不启动/连接现网网关。打印全部事实七字段（quote/source_loc 展开）、真实往返（含简报导览）、疑点和终态；partial 明确非零退出 2，不伪称成功。原件只读、只写新根，原件与两真库前后只读快照比较。原始模型报文只在显式新根/model-trace 下存储，无秘钥。

涉及文件：app/document_read/service.py、app/employees/model_tool.py、scripts/dev/run_m5b_real.py、skills/file-processor/SKILL.md、正常生成的 harness-openclaw/skill-registry/catalog.json、tests/test_document_read.py、tests/test_m5b_real_runner.py、tests/test_phase1_catalog.py、本 report。未触碰现网、业务解析/财务链；未提交 harness-openclaw 运行时记忆。

## 如实保留迭代失败，不把中间失败说成通过

所有日志/根保留，不删除：原诊断最后缺简报退出 1；`.runtime/m5b-bugs-full.log`（12 次事实请求仍 partial）、`m5b-bugs-full-final.log`（16 次仍 partial）暴露无工作队列导致重复扫描，随后补员工队列；`m5b-progress-full.log` 暴露可选 value_type 误拒与简报数字出处错误；`m5b-verified-full.log` 暴露员工加 L: 前缀错误引用，护栏拒绝为 partial；`m5b-final-budget.log` 是范围结束行 421 超过实际 401，随后增加上下界提示与一次结构纠正；`m5b-budget-verified.log` 首轮 partial 保留 8 条，续跑导览把字符串引用输出成对象，全拒而 partial，原始报文 `/tmp/sam-m5b-real-y46aokur/model-trace/`，随后增加一次计费格式纠正。没有后台修正引用、造事实或改脚本硬凑答案。

中间全预算真实成功日志 `.runtime/m5b-final-full.log`，根 `/tmp/sam-m5b-real-5mr4qf_8`：classify/attribute/extract=1/1/11，done / l2_committed=1，简报导览均生成，147 条已核验事实。表中九个财务数逐年正确（2022/2023/2024）：营收 10543.93/15099.24/11413.05 万元，研发费 601.20/863.19/626.49 万元，占比 5.7/5.72/5.49%。正文 14.86 留疑点，不强行统一。5 条 unresolved 含故意坏记录及 4 条 quote 拒绝。该次在后续结构护栏补充之前，最终版本以以下复跑为准。

## 最终版本实际复跑（见续填结果）

运行环境现在具备 socket/bwrap 权限，真实调用 qwen3.8-flash，没有再以壳限制跳过真机。命令：
```
scripts/dev/run_m5b_real.sh --inject-bad
scripts/dev/run_m5b_real.sh --budget-demo --inject-bad
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_phase1_catalog.py tests/test_document_read.py tests/test_m5b_real_runner.py
.venv/bin/python -m compileall -q app scripts tests
git diff --check
bash -n scripts/dev/run_m5b_real.sh
```
技能/模型调试报文只在新隔离根；模型端点/秘钥环境口径沿用下一节，输出默认现改 8192 tokens，三阶段预算默认 2/2/16。无外部服务变更、无 push。短文档允许小批交付不等于实现长文档任务，本期不扩展长文档链路。

最终版本低预算命令退出 0，日志 `.runtime/m5b-budget-final-current.log`，产物 `/tmp/sam-m5b-real-aiai9gf4`：首次 classify/attribute/extract=1/1/1，partial，已核验事实保留 16 条；提高累计预算同 job 续跑，最终 1/1/11（合计 13 次真实请求），143 条已核验事实，done / l2_committed=1 / brief_generated=true / guide_generated=true。2 条事实 unresolved（含故意注入坏记录），10 条初版简报/导览坏条目被出处护栏隔离，计费格式纠正后生成合法投影，不放宽校验。上述财务九数均正确，年份 2022/2023/2024，金额万元、比例 %；14.86% 在 todos 保留正文疑点。专利表坐标 docx:body/88/table 未有 number 型事实。断网逐格质检 pass，6 对象原位链接全部能取回；脚本 protected before==after 为 true。

当前全量 `.venv/bin/python -m pytest -q` 实测 **990 passed / 1 skipped / 0 failed，121.36s**（`.runtime/m5b-current-pytest.log`）；三个指定测试文件合计 **39 passed，0.61s**。新增包括：零候选/全拒不得 done、超时 partial 保存和续跑、员工下标归属、原始 trace 不含秘钥及根范围/0600、队列进度与错误队列、可选 value_type 保守行为、坏简报兄弟隔离、未知期间、逐行引用范围、一次范围纠正/重复越界停止、JSON 标量序列化、一次导览引用格式纠正，未删除/弱化原用例。

最终版本全预算命令 `scripts/dev/run_m5b_real.sh --inject-bad` 退出 **0**，日志 `.runtime/m5b-full-final-current.log`，根 `/tmp/sam-m5b-real-sjyn3qkd`：classify/attribute/extract=**1/1/13**（合计 15 次真实请求，含计费格式纠正与简报导览），171 条已核验事实，done / l2_committed=1 / brief_generated=true / guide_generated=true。九个财务数、2022/2023/2024 年份及单位与以上答案一致；14.86% 正文疑点保留。日期/专利编号未出现于 number 事实，专利表 number 条数 0（专利数量仍是量值，不等同编号）。8 条 unresolved：故意坏记录 1 条，另外 7 条原文引句使用省略号被拒；5 条初版阅读条目被隔离，格式纠正后合法条目正常投影。原始报文最高单次 38.6271 秒、输出 3248 tokens，所有 finish_reason=stop，非流式。6 对象三平与取回、独立断网质检均通过，脚本 protected before==after=true。

最终 compileall（app/scripts/tests）、bash -n 与 git diff --check 均退出 0。施工前后只读快照：`.runtime/m5b-bugs-before.json` / `.runtime/m5b-bugs-after.json`，命令 `.venv/bin/python scripts/db_baseline.py data/app.db /tmp/sam-m5/main.db --evidence-root /tmp/sam-m5/material`；`cmp` 退出 0，两真库 SHA256、mtime、各表行数及 material 全树哈希一致。保留 data/app.db 原有 5 列，没有迁移/重建它；18790 未访问，原件未修改。

遗留：真实模型仍可能遗漏事实、改变非目标事实的展示格式或给出无效引句/归属/阅读引用；这些不是后台规则可以“补对”的，本轮明确保留拒绝和人工疑点，不声称全文业务准确性已验收。单次传输超时仍可能发生，但现能 partial 并保留成果；同 job 的 service.run 支持续跑，脚本预算演示已证明，脚本每次 CLI 启动仍建新根（未增加任意旧根重用入口）。长文档、旧链切换/退役不做。提交只在本地，本节所属 commit 见交付回执或 git log；等待饭团复核，不自行宣布黑盒验收通过。

---

# M5-B 一键真机脚本与技能登记修复（2026-10-13，待饭团真机）

## 如何运行

正常 WSL（有 socket/bwrap 权限，仓内 `.venv` 与 `.sam-isolated/venv` 均已安装）：
```
scripts/dev/run_m5b_real.sh
scripts/dev/run_m5b_real.sh --budget-demo --inject-bad
```
真实原件默认 `/tmp/sam-m5/material/fmt_C_info.docx`，可用 `--source <只读docx>` 指定另一份短文档。每次强制 `mkdtemp(prefix="sam-m5b-real-")` 建立全新隔离根，不能通过环境默认库复用或指定真实库。产物保留供饭团检查，不删除原件。脚本直接真实调用 EmployeeModelTool，不启动或连接任何 OpenClaw 网关；bind:0 隔离端口整个运行期间保留，并注入父/子环境，因此不使用 18790。脚本未声称这个监听器是运行中的 AI 网关。

环境：`SAM_LEADER_MODEL_API_KEY`（或 `SAM_MODEL_API_KEY_ENV` 指定的变量），兼容 `SAM_GUIDE_MODEL_API_KEY` / `~/.bl_tokenplan_key`；不打印秘钥。`SAM_LEADER_MODEL_BASE_URL` 可覆盖项目模型端点；`SAM_LEADER_MODEL` 默认显式 qwen3.8-flash。`SAM_EMPLOYEE_MAX_OUTPUT_TOKENS` 默认 8192（本轮修复前为 32768），允许覆盖。数据类 SAM 路径与任意 SAM_*DB 均被改写到新根；HF/Torch/XDG 缓存也在新根。原有 G1 audit 禁止临时根外任何写入，子进程额外 bwrap 宿主只读/仅新根可写。没有 socket 权限时应失败，不降级、不改固定端口。

输出口径：
1. 输出**全部员工事实**（entity/attribute/period/value/unit/source_span，source_span 包含 quote/source_loc），由饭团找金额及占比对答案；没有后端财务词表、指标映射或写死答案。
2. classify/attribute/extract 是实际 transport 尝试次数，失败也计数、无隐藏重试。extract 包括同员工简报/导览两次请求，故总数不会漏计；没有真实调用时不能当真机证据。
3. 打印 job 状态、l2_committed、简报/导览是否生成、模型名、对象原位链接数及各对象取回大小；保存 document.md、地图/资产清单/result.json。
4. `--budget-demo` 首次将事实阶段预算压到 1；已核验事实入库后在后续调用前熔断，打印 partial 和保留事实数，随后同 job 提高到累计预算恢复，不重做定性/归属。若员工没有产出可直接核验事实，脚本明确失败，不伪称演示成功。
5. `--inject-bad` 在第一次真实事实响应追加一条缺字段记录（不是编造业务事实），它转 unresolved/todo；其余引用通过的事实继续入库/投影。末尾检查坏数据演示确实发生。
6. 前后 before.json/after.json 使用 G1 只读快照，比较 data/app.db、/tmp/sam-m5/main.db 与输入原件 SHA256/mtime/表行数；不一致 exit 非零。真实财务正确性、调用量和沙盒执行由饭团在正常 WSL 真机验收，不在 Codex 壳伪造结果。

## 修复与隔离审查

- file-processor 追加合规 frontmatter，正文中文契约未改；走 `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py` 正常生成，结果 **8 skills / 0 rejected_skills / 8 tools / 1 rejected_tool**。catalog.json 已正常收录；rejected.json 正常再生仍为 []，字节未变，不手改拒收掩盖问题。
- 新增 `scripts/dev/check_m5b_offline.py` 是限定文件的无网络回归入口，不替换全量 conftest。它先用同一 G1 data_keys/self_check/install 将所有写限制在独立临时根，然后拒绝**全部 socket 与全部 subprocess/fork**，以比原策略更严格的方式运行纯 Python 用例。仅此入口使用 --noconftest，避免无意义的随机端口申请；原 conftest 与网关测试未改。
- 修复新旁路事务失败时不得把未提交候选记为完成的断点问题；置信度 NaN/Infinity 拒绝作为结构错误。逐格质检脚本可在离线单测用临时证据验证，生产入口只在既有断网 QC sandbox 中运行。

## 实测状态与当前待确认点

饭团提供的上轮结果：test_document_read **13 passed**；全量 **966 passed / 2 failed / 1 skipped**（饭团环境，本壳没有重跑全量）。

本壳无网络专项最后一次：`.venv/bin/python scripts/dev/check_m5b_offline.py` => **20 passed / 1 failed / 0 skipped**。catalog 产物一致与 rejected 输出一致断言已过；剩余失败是 `test_skill_catalog_matches_generated_artifact_and_records_rejections` 的精确技能名集合仍只含旧 7 个名字，正常收录 file-processor 后必有 8 个。用户要求禁止删改该测试，所以未擅改；已请求仅追加预期名字（保留全部精确断言），等待明确确认。随后新增两条事务失败/transport 计数回归，当前尚未复跑，不把它们计为通过。

`.venv/bin/python -m compileall -q app scripts tests`、`bash -n scripts/dev/run_m5b_real.sh`、`git diff --check` exit 0。`.runtime/m5b-script-after.json` 与施工前 `.runtime/m5b-before.json` cmp exit 0：两个真库及 material 所有原件完全未变。没有重跑任何需要 bind 的用例，真实模型调用仍为 **0 次**。真机由饭团执行；不宣称全量已绿或完整 M5-B 已通过。

本地提交结果见回执；不 push，不提交 harness-openclaw/DREAMS.md 或 memory/dreaming 等无关运行时改动。旁路未挂现网上传、未退役碎块旧链、未施工长文档。

---

# M5-B 单文件直读 · 施工中断记录（2026-10-13，未交付）

已重读同一绝对路径方案末尾 §9（331 行版），按唯一文件处理员施工；未接入现网上传链、未删除旧链、未改网关、未开始长文档。**本节是未完成状态记录，不是验收通过报告。**

## 本轮暂存改动（尚未 commit / 未 push）

- `app/document_read/{__init__,dossier,quality,service}.py`：旁路骨架、按原件坐标读取、任务对象哈希/路径检查、独立断网逐格质检入口；唯一员工按三阶段计实际尝试次数；SQLite 工作进度和已核验事实同事务保存；预算中断保留事实、恢复去重；坏候选按条隔离。模型未赋予代码执行或自发 pass 权限。
- `skills/file-processor/SKILL.md`：唯一员工任务契约；财务日期/标识、年份表头、引句、冲突保留等只写技能，不加后端业务规则。
- `skills/document-ingest/scripts/verify_direct_docx.py`：只读 OOXML 表格位置/逐格内容和对象清单校验，限定在既有 QC sandbox 中执行。尚未完成真实沙盒回归，不声明已可交付。
- `tests/test_document_read.py`：首批旁路、引用失败隔离、预算保留/恢复、幂等、宿主作用域与禁止自发通过的回归草稿。

## 实际测试与环境阻断

首次 `.venv/bin/python -m pytest -q tests/test_document_read.py`：**2 passed / 9 failed**。9 例均失败于新适配器读取导览输入时用了 `l1_items`；现有契约是 `l1.items`。已修正，但未获成功复验，不将修正视作通过。

第二次相同命令：**exit 4，conftest 加载失败，未执行用例**：
```
ImportError while loading conftest '/home/jakki/projects/startup-ai-manager/tests/conftest.py'.
tests/conftest.py:34: in <module>
    with socket.socket() as listener:
/usr/lib/python3.14/socket.py:236: in __init__
    _socket.socket.__init__(self, family, type, proto, fileno)
PermissionError: [Errno 1] Operation not permitted
```
这是创建随机端口 socket 的环境权限错误；不改 G1 隔离、不删除自检、不换固定端口、不绕过权限，也未继续硬重试。已即时报告并请求恢复可用 WSL 执行环境。真实模型往返 **0 次**，未运行真实 Word 业务实验，不能验证财务答案或调用量验收。

`.venv/bin/python -m compileall -q app scripts` 与 `git diff --check` 均 exit 0；编译成功不代表业务回归通过。

## 真库/原件只读证据

修改前 `.runtime/m5b-before.json`，中断后 `.runtime/m5b-after-blocked.json`；命令：
```
.venv/bin/python scripts/db_baseline.py data/app.db /tmp/sam-m5/main.db --evidence-root /tmp/sam-m5/material
cmp .runtime/m5b-before.json .runtime/m5b-after-blocked.json
```
`cmp` exit 0，两真库 SHA256/mtime/表行数及 material 全部文件 SHA256/mtime 完全相同。原件未修改、未删除；未操作 18790 网关。

## 待续（不能放行）

完整黑盒入口/隔离实跑脚本、真实对象工具接线、投影故障根因及逐条坏数据修复、真实财务与低预算实验、真实沙盒核验、完整回归、自审与本地提交均尚未完成。新文件只为未接线施工草稿；原有 runtime 记忆改动保持不提交。恢复执行环境后继续原单，不做切换/旧代码退役。

---

# G1 数据迁移与测试隔离 · 结构加固（2026-10-13）

## 交付与放行边界

只做底座护栏，未施工 M5-B/B5/C/D/E；没有修改解析/财务判断、角色权限、业务词表/映射/公式。
只本地提交，未 push；本报告所属 commit hash 见最终回执。
**本单不能宣称全过程满足“未连接生产端口”硬约束**，见下方偏差披露；代码回归通过也不替代饭团真机黑盒放行。

## A–F 实现

- **A 显式迁移**：新增 `python -m app.db.migrate --db <明确目标>`，无 `--db` 为 argparse exit 2，不解析/连接任何默认库。`PRAGMA user_version` 为 SAM schema 版本；当前版本再次执行只校验、直接返回（字节/mtime 不变）；未来版本拒绝降级。Flask import/启动、SAM CLI、记忆 provider、云 SnapshotStore 均不自动建表/ALTER/写种子，只校验版本并提示显式命令。原记忆表/触发器/旧投影失效标记移入显式迁移；staging/audit 的首用 CREATE 也搬入 schema。测试显式初始化临时路径，旧业务断言保留。
- **B fail-closed**：删除 `data/app.db` 代码兜底；`init_db` 必须传路径，`connect()` 需要明确 `SAM_DATA_ROOT`。标准/云 shell 入口缺数据根立即拒绝。模块配置允许现有显式 profile 的 `paths.data_root`；覆盖根后子路径随根派生。云只读也删除 `/tmp/sam-cloud-home` 隐式路径，需显式根或 `paths.cloud_data_root`，不能借用 local profile 的真实根。缺库/旧版本先报错，不去创建目录或业务表。
- **C 隔离纵深**：收集前重定向数据类 SAM 路径、任意 `SAM_*DB`，两次启动自检；专属 TMPDIR、pytest basetemp、cache，退出清理本轮临时根。父 audit 保护 data/、/tmp/sam-m5、重定向前 DB 地址，并拒绝本轮临时根外写入；覆盖文件 fd、目录 fd 与正确的 symlink 目标语义。普通 subprocess 强制 bwrap（宿主只读、仅本轮根可写、真实路径只读遮罩），Python 子进程另继承 audit；`-I` 仍靠内核拒写。无 bwrap 不降级。QC 原有任务级沙盒不改，只补真实根不可见/不可写证明，仍只挂本任务证据。
- **D 端口**：conftest 与 bootstrap 专项各选随机空闲端口，run.sh 尊重显式端口覆盖；父/普通 Python 子进程 audit 拦截 18790。生产 pid 152636 的 18790 一直保留，没有杀进程/重启/改配置。普通 subprocess 为真实 localhost HTTP 回归保留网络，不声称任意恶意 native 程序的网络都已隔离；QC 仍独立禁网 namespace。
- **E 只读基线**：`scripts/db_baseline.py` 输出 SHA256、mtime_ns、全部表行数；immutable/ro SQLite，不导入应用、不 checkpoint、不写库；有非空 WAL 时拒绝误导性快照。`.runtime/` 已忽略，保留本轮本机证据。
- **F 底稿**：运行期归档使用明确 `SAM_2A_ROOT`，未设则拒绝；外部源材料由快照 `--evidence-root` 明确指定并逐文件哈希。`/tmp/sam-m5/material` 易失/备份风险已写 `docs/data-lifecycle.md`；本期不搬迁、不删原件、不写该根。

## 开工与完工只读对照

开工 `.runtime/g1-before.json`（实际开始改数据库代码之前生成）：

| 对象 | SHA256 | mtime_ns |
|---|---|---|
| data/app.db | `705b0afb00b2f3291d4cd01cb7191d847bee7604a95b3b7687d0873ddc663661` | `1791827065816621604` |
| /tmp/sam-m5/main.db（现有空文件） | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `1791816215160617149` |
| /tmp/sam-m5/material/fmt_C_info.docx | `6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965` | `1791806544388616710` |

正式 app.db 全表计数：dashboard_preferences=0、doc_types=24、entity_bridge_artifacts=1、entity_bridge_blocks=2、entity_bridge_runs=0、entity_roster=1、fact_runs=1、facts=0、file_classifications=0、file_tags=0、m1_chunks=0、m1_directories=0、m1_documents=0、m1_projection_config=1、m1_projection_state=0、memory_kv=0、modules=10、parse_jobs=0、parse_staging=0、source_edits=0、source_files=0、sqlite_sequence=8、todos=0。sam-m5/main.db 无表。

开工/中途/真实 import 后/最终快照 `cmp` 均相同（全部行数、SHA256、mtime_ns）。保留正式库原五列，不回滚/不重建，不写 schema 版本号；两个真实库均仍是 version 0。分别显式指向这两库 import app/db、memory/provider、webapp.app，均 exit 1，提示 expected 1/运行显式迁移；前后快照不变。**不要为了启动新代码而自行对这两个真实库跑迁移**，处置仍由饭团决定。

另补底稿根逐文件快照 `.runtime/g1-evidence-before.json` / `g1-evidence-after.json`，cmp 相同；这份全根快照是施工中扩展补录，不能冒称覆盖了其补录前所有文件的形式化零变化证明。开工 DOCX 的严格前后对照单独成立。

## G 新增回归与真实测试

新增 **30 个用例**：`tests/test_g1_guardrails.py` 29 个（含参数化计数），`tests/test_document_quality.py` 1 个。

- 无根 connect/标准启动/无配置 profile 拒绝；初始化无默认目标；缺 schema 在 Gateway prepare 前拒绝。
- migrate 无 --db 拒绝；临时库两次字节/mtime/SQL dump 相同；拒绝未来版本降级。
- 缺库/旧版本校验不建库不 ALTER；旧库 import 拒绝但不改库；当前库 import 字节/mtime 不变。
- conftest 环境自检和子进程继承；3 个不合格数据键拒绝；原 SAM_CUSTOM_DB 地址先保护再重定向。
- 3 个真实/非临时路径父写拒绝；2 个真实库的普通 Python 与 -I 子进程拒绝可写打开。
- 父/子进程生产端口审计拒绝、测试随机端口断言。
- fd/dir_fd 写审计拒绝（只触发审计事件，不执行真实 chmod/remove）。
- 只读基线原件/库不变、live WAL 拒绝、明确材料根逐文件哈希。
- snapshot 显式迁移幂等/构造只读；QC bwrap 只见任务证据，看不到且写不到真实根。

新增用例完整节点（实际 collect-only：30 collected）：

```text
tests/test_g1_guardrails.py::test_no_data_configuration_rejects_connect
tests/test_g1_guardrails.py::test_unconfigured_standard_startup_is_rejected
tests/test_g1_guardrails.py::test_empty_profile_rejects_unconfigured_runtime
tests/test_g1_guardrails.py::test_migrate_requires_explicit_db
tests/test_g1_guardrails.py::test_migration_cli_twice_is_identical
tests/test_g1_guardrails.py::test_missing_or_old_schema_validation_never_creates_or_alters
tests/test_g1_guardrails.py::test_application_import_with_legacy_db_does_not_mutate
tests/test_g1_guardrails.py::test_application_import_with_current_schema_is_readonly
tests/test_g1_guardrails.py::test_isolation_environment_self_check_and_inheritance
tests/test_g1_guardrails.py::test_self_check_rejects_unisolated_data_key[SAM_MAIN_DB]
tests/test_g1_guardrails.py::test_self_check_rejects_unisolated_data_key[SAM_2A_ROOT]
tests/test_g1_guardrails.py::test_self_check_rejects_unisolated_data_key[SAM_OTHER_DB]
tests/test_g1_guardrails.py::test_parent_write_audit_denies_protected_and_non_temporary[/home/jakki/projects/startup-ai-manager/data/app.db]
tests/test_g1_guardrails.py::test_parent_write_audit_denies_protected_and_non_temporary[/tmp/sam-m5/main.db]
tests/test_g1_guardrails.py::test_parent_write_audit_denies_protected_and_non_temporary[/home/jakki/g1-never-create]
tests/test_g1_guardrails.py::test_native_and_python_children_cannot_open_protected_db[/home/jakki/projects/startup-ai-manager/data/app.db]
tests/test_g1_guardrails.py::test_native_and_python_children_cannot_open_protected_db[/tmp/sam-m5/main.db]
tests/test_g1_guardrails.py::test_production_gateway_connection_denied_parent_and_child
tests/test_g1_guardrails.py::test_bootstrap_port_is_private_random
tests/test_g1_guardrails.py::test_baseline_script_reads_without_changes
tests/test_g1_guardrails.py::test_explicit_evidence_root_required
tests/test_g1_guardrails.py::test_explicit_initializer_has_no_default_target
tests/test_g1_guardrails.py::test_migration_refuses_newer_version_without_changes
tests/test_g1_guardrails.py::test_original_db_environment_path_is_protected_before_redirect
tests/test_g1_guardrails.py::test_readonly_baseline_refuses_live_wal
tests/test_g1_guardrails.py::test_snapshot_migration_and_constructor_are_readonly_on_reopen
tests/test_g1_guardrails.py::test_explicit_material_root_hashes_each_file_without_writes
tests/test_g1_guardrails.py::test_protected_descriptor_and_directory_relative_audit_are_denied
tests/test_g1_guardrails.py::test_startup_rejects_missing_schema_before_gateway_preparation
tests/test_document_quality.py::test_g1_bwrap_exposes_only_task_evidence_not_real_stores
```

原有 925 passed / 1 skipped 用例未删；与显式迁移冲突的旧 fixture 改为显式临时准备，拒绝外部 rerank 的测试仍直接调用真正未初始化 provider，未放宽断言。

测试记录（真实，失败不隐藏）：
- 首轮相关：68 passed / 2 failed（旧 provider 自动初始化假设）；调整为显式准备。
- 改造中全量：884 passed / 1 skipped / 36 failed / 10 errors；旧 fixture 缺显式迁移、audit 的 symlink/dir_fd 语义、真实 harness inbox 写假任务等均修正，没有放宽真实路径保护。
- 中间全量：951 passed / 1 skipped / 0 failed（120.70s）。
- 后续全量：954 passed / 1 skipped / 0 failed（122.87s）。
- 增加 Gateway prepare 前校验期间的一轮全量：953 passed / 1 skipped / 1 failed（121.30s）；执行中的旧 fixture 尚按两步启动断言，已同步为“校验→prepare→Flask”。最终冻结代码完整复跑：**955 passed / 1 skipped / 0 failed（120.47s）**，共 956 collected，比 925 基线新增 30 个用例；同一真实网关仍在运行。
- 严格 G1/质检/云/启动专项：90 passed（19.78s）；最后的 G1/启动/网关编排专项：61 passed（6.51s）。
- 默认命名未收集的 M1 记忆专项另跑：80 passed（5.38s），不计入全量新增数字。
- compileall（app/scripts/webapp/tests）、git diff --check、staged diff --check 均 exit 0。

## 本轮偏差披露（生产端口）

首轮新增子进程端口回归在 child audit 尚未接线时做了一次 TCP connect 到 18790，进程成功返回，测试因预期拒绝而失败。**这是本单不连接生产端口要求的实际偏差**；没有发业务请求、停/重启/修改网关，但不能声称全过程零接触。已及时说明，并改为子进程仅触发 `sys.audit('socket.connect', ...)` 的自检，缺护栏也不会真连生产。最终套件通过不抹掉这次偏差，是否接受本单须饭团判断。

提交后最后一次只读 `ss` 核验报 `Cannot open netlink socket: Operation not permitted`，未重试/未绕过权限；此前 `ss` 已确认 18790 的监听者为 pid 152636。提交后另用只读 `ps` 确认同一进程仍在；不把该项当作提交后重新验证了端口归属。

## 新旧启动方式与停手

旧：无配置/import 自动初始化默认库。
新：先明确 SAM_DATA_ROOT/SAM_MAIN_DB/SAM_2A_ROOT，在**经过确认的新临时库**执行 migrate --db，再启动；云 snapshot 另用 --snapshot。命令示例、快照/隔离边界见 `docs/data-lifecycle.md`。

harness-openclaw/DREAMS.md 与 memory/dreaming 等原有运行时变更保持不提交、不删除；本单提交只含 G1 文件，不能把原有脏工作区冒称全仓干净。完成后停手等饭团真机黑盒，不进入 M5-B。

---

# M5 接续：A2 代码/安全实测交付与正式库事故（2026-10-13）

## 放行状态（先读）

**A2 代码与隔离实测已交，但不能宣称满足本单“正式库零污染”硬约束，暂不可放行。** 本轮发现正式 `data/app.db` 被自动初始化/迁移，出现 5 个新增质检字段。未擅自回滚；请饭团决定正式库处置与后续黑盒放行。已停止在 B/C/D/E 上施工。

本地提交：
- `6ff9e82fe966170cd535903fead10b40617bb922` — A：DOCX 合格md独立预览（补交）。
- `21fb5609a4cb4472c0417be0633bc6e4d923ea35` — A：接通DOCX合格md与保真产物。
- 本报告所在 A2 独立提交 hash 见交付回执；没有 push。

## 正式库事故与整改

- 正式库 mtime 变为 **2026-10-13 01:44:25.816621604 +0800**，位于本轮施工时段。
- 只读核对确认 `parse_jobs` 已有 `quality_status / quality_model_calls / quality_script_calls / quality_reworks / quality_issues` 五字段。本轮早期专项未给父进程注入完整数据根，默认初始化路径未被有效隔离；不应把 fixture 内 monkeypatch 当成 import 前隔离。
- 当前只读计数：`source_files=0 / facts=0 / parse_jobs=0`。**没有开工前主库快照，以上不是完整前后零污染证明，也不推断其他表完全未变。** 未尝试删除字段、还原或写正式库。
- 新增 `tests/conftest.py`：测试收集前创建独立临时数据根，覆盖所有数据库、记忆、对象/图片路径；Python audit 拒绝正式 `data/` 的写操作和可写 SQLite 打开。避免显式配置/default 路径绕过时继续污染。子进程继承临时环境。
- 防护后的全量：**925 passed / 1 skipped / 0 failed（124.11s）**。本轮不能据此追认前面的误迁移合法或已经回滚。

## 文件/实现清单

| 文件 | 内容 |
|---|---|
| `app/document_quality/sandbox.py`（新） | 角色限定受控 Python 通道、结论 schema、一次性 bwrap、输入/输出/CPU/时长/磁盘/内存护栏、取消与清理 |
| `app/document_quality/gate.py`（新） | 文档处理员动作循环，核验/看快照/结论，直接核验前置，独立预算、定位核验、有限重转、失败转 needs_human |
| `app/document_quality/service.py`（新） | 接入任务/原件/解析产物，持久化结论、问题单、分项计数；pass 才返回下游 |
| `app/document_quality/snapshots.py`、`__init__.py`（新） | 固定宿主脚本在同款离线隔间渲染原件页面；LibreOffice/PyMuPDF 216dpi、字体/cache 版本管理 |
| `skills/document-ingest/QUALITY.md`（新） | 纯保真质检技能：成熟库优先、数字/列对应/完整性、告警复核、缺陷复现证据、禁止业务事实认定 |
| `skills/document-ingest/SKILL.md`、skill catalog | 真实产物契约与质检边界；员工任务文案不含开发代号，catalog 摘要同步 |
| `app/employees/model_tool.py` | 受控快照多模态输入；拒绝宿主/外部图片 URL；原文本员工协议不变 |
| `app/harness/runtime/openclaw_adapter.py` | 重转接收坐标问题单、私有任务结束清理；转换员工任务文案清理 |
| `webapp/app.py`、`app/parse_jobs.py` | 转换后、任何语义员工之前的 reviewing 门；needs_human 终态与人工重试 |
| `app/db/schema.py`、`database.py` | 五个独立质检状态/计数字段及旧库初始化迁移（本轮误触正式库的相关代码，事故未隐藏） |
| `webapp/templates/inbox.html`、CSS | 同主题显示质检计数/坐标问题单、需人工状态与重试，不改变交互形态 |
| `requirements-qc.txt`、`requirements.txt`、build 脚本 | 固定质检六库及其依赖；qc-only 只复制 requirements 与本机字体，不复制 checkout/客户数据；通过 --install 装原有 parser+质检环境 |
| `tests/test_document_quality.py`（新） | 24 个实测/协议用例：真实隔离、额度、库、错列/拆 run、有限重转、拒绝放行、持久状态、原件页面、多模态传输 |
| 三个已有业务/投影测试 | 只 stub 新增质检依赖，因为原 fixture 是假二进制；原业务/投影断言保留，新增专门真实沙盒与拒绝下游测试 |
| `tests/conftest.py` | 收集前路径隔离与正式 data 写拒绝；事后防再发生，不替代事故处理 |

`tests/test_runtime_bootstrap.py` 与 `tests/test_phase2_tool_search.py` **没有修改**，其 codeMode=false 强制断言仍通过。角色/工具权限配置未打开全局或其他角色代码执行。

## 沙盒边界与实测

普通 UID 1000 同权隔离，**未创建 sam-runner**；拒绝 root，实例内 UID 也实测非 root。复用现成 bwrap 0.11.1 参数模板，未引入其他沙盒/内核/执行链。

- `--unshare-all --unshare-user --unshare-net --disable-userns --cap-drop ALL --die-with-parent --new-session --clearenv`；只读系统库、公开 font/LibreOffice 配置、隔离 venv、本任务证据和脚本。没有挂宿主 home、凭据、仓库或其他公司目录。
- 只复制本任务原件字节与允许的产物到 0700 一次性证据目录，再只读 bind；对象仅限当前文件 hash 子目录并核 SHA256；原件哈希校验。不挂原件父目录。
- 根 filesystem 与 `/dev` 只读；唯一可写盘是 `/work` 的 **16MiB 限容量 tmpfs**；`/tmp` 只是它的别名。宿主临时证据/输出由宿主管理，结束/失败/取消后删除；员工不能拿任意输出路径。
- 默认核验墙钟 30s、CPU 10s（继承 rlimit + 整棵进程树计数）、单进程地址空间 1GiB；输入 128MiB、输出 128KiB；进程树超过 64 进程终止。CPU/进程监测为 10ms 周期，磁盘容量由内核 tmpfs 硬限制。

| 实测项 | 结果 |
|---|---|
| 改 `/evidence` 原件 | 被拒；字节未变 |
| 写正式库路径、`/usr`、根目录 | 被拒 |
| 读/写另一任务宿主路径 | 被拒 |
| 连接公网 IP:443 | 失败；新 network namespace，默认无联网 |
| 财务/法务/业务分析员/manager 调用核验通道 | 全部 PermissionError；未创建工作区 |
| CPU 死循环、并行子进程 CPU | 超限终止；临时任务目录清空 |
| sleep 超墙钟、输出洪泛 | 超限终止；临时任务目录清空 |
| 多文件累计写满 tmpfs | ENOSPC；任务目录清空 |
| 六个成熟文档库 | 隔间内离线 import + 版本断言通过 |
| 全局 codeMode / 其他角色 | false；原两测试未改、真实通过 |

核心库版本：python-docx 1.2.0 / openpyxl 3.1.5 / python-pptx 1.0.2 / pdfplumber 0.11.9 / PyMuPDF 1.28.2 / Pillow 12.3.0。为保持现有 isolated parser 契约，通过同一 --install 入口补齐仓库原有 Docling 2.130.0 / Torch 2.14.0+cpu 等依赖；pip check：No broken requirements found。运行期无 pip 或联网安装。

## 真实原件与质检正向实测（不冒充在线员工）

- 原件仍是 `/tmp/sam-m5/material/fmt_C_info.docx`；SHA256 不变。经**隔离 venv 的实际 document-ingest/Docling** 输出 parsed，canonical md 与饭团通过的预览 `cmp` 一致；18 表、6 对象，独立引擎结果保留。
- 真实 bwrap 中 `python-docx.Document('/evidence/original.docx')` 直读：合格表16 → `pass`；人为交换营收行两列 → `rework`，问题坐标 **docx:body/99/table/row/1**，期望行从原件单元格取得。
- 证据：`/tmp/sam-m5/a2-check/real-qc-results.json`；复现核验脚本 `verify_real.py`；这是**真实隔离/成熟库核验，没有调用在线模型**（实际模型请求 0）。自主模型质检待饭团黑盒，不伪称实测通过。
- 模拟员工动作协议测试：原件数字由两个 run 组成，成熟库读为 15099.24；错列第一次 rework，重转一次后 pass；连续不合格超过限次 needs_human；实际 web 门拒绝后语义员工未执行；已持久化问题/计数、l2_committed=0。
- 默认为质检尝试预算 8、重转最多 1（可配 `SAM_QC_MAX_MODEL_CALLS/SAM_QC_MAX_REWORKS`），失败尝试也占预算；这些是 A2 防空转护栏，**不是 C 阶段实测标定的推荐预算**。模拟协议 4 次调用不可当作真实 API 往返或费用数据。
- 文本模型沿既有配置，可用 `SAM_QC_MODEL`；按动作查看快照才带图片，视觉模型默认已指定的 qwen3.8-flash，可用 `SAM_QC_VISION_MODEL` 覆盖。没有给其他员工增加代码执行。

## 页面快照与失败修复

1. 探针 `--disable-userns requires --unshare-user`：补显式 user namespace，不降级执行。
2. LibreOffice `no valid pipe path found`：将 `/tmp` 指向唯一限额 `/work`。
3. UNO RuntimeException：只读挂入查实的 `/etc/libreoffice` 公开 registry 链接目标，不挂整个 /etc。
4. 人眼复核发现中文方框：安装于本机的微软雅黑/宋体等字体复制到 ignored `.sam-isolated/fonts`，只读挂入，私有 fontconfig/cache；未下载/未入 Git。修后页面中文可读，已查看 page-001。

当前原件代理渲染生成 **14 张 216dpi PNG**，路径：`/tmp/sam-m5/a2-check/map/65/<hash>.pages/`。这是 LibreOffice 本地代理分页，**不宣称与 Windows Word 真机页码/版式完全一致**。缓存含 renderer_version/font_signature，字体改变必须重新生成。快照无法生成时明确告警，技能要求拿不准转 needs_human，不能假装已看。

## 测试与交付边界

- 新增质检/renderer 专项最末独立执行：35 passed（3.51s；随后新增 web 拒绝依赖用例纳入全量）。
- 原两个强制断言 + 业务/投影专项：107 passed（35.85s）。早期原端口专项：34 passed / 1 failed（18790 被未验证进程占用），没有杀现有进程；改空闲端口 46437。
- 隔离全量首跑：920 passed / 1 skipped / 1 failed（74.68s）；原因是新隔离 venv 缺 docling，修的是依赖，不改断言/跳过。
- 补依赖后记录：923 passed / 1 skipped（116.80s）；新增传输/拒绝用例后 925 passed / 1 skipped（115.51s）；字体修正后 925 passed / 1 skipped（114.63s）。
- **最终，新增收集前隔离与正式 data 写拒绝后：925 passed / 1 skipped / 0 failed（124.11s）。** skip：真实样例 opt-in 环境参数为空，真实 Word 已另跑独立实测。
- compileall（app/scripts/webapp/文档脚本）、git diff --check 均 exit 0。
- 当前保留隔离证据与运行环境给饭团复验；本任务临时核验工作区为空。只清本轮自建测试数据库，不删真实底稿/原件对象，不清用户已有 `/tmp/sam-m5` 主库。
- B 新分析链、B5 财务技能、C 成果保留/续跑、D 坏事实投影隔离、E 碎块退役均未施工；pass 后仍走原有后续业务链，原先预算/投影问题不能拿 A2 结果掩盖。
- **正式库五个新增字段处置，以及在线员工/VLM/真机 UI 黑盒，由饭团决定；未经确认，不进行正式库写或回滚，不宣称放行。**

---

# M5 接续：A 解析产物接线（2026-10-13）

- 饭团已黑盒通过 A 独立预览；上轮只读 `.git` 阻塞已解除。
- 已补提交 `6ff9e82fe966170cd535903fead10b40617bb922`（`A：DOCX 合格md独立预览`），未 push。
- 本接线提交覆盖：`skills/document-ingest/scripts/bridge`、`docx_fidelity.py`、解析 manifest schema、`app/storage/mapping_store.py`、`tests/test_m5_docx_fidelity.py`、本报告。
- Word 的真实 document-ingest 输出新增 `document`（整份 Markdown/地图/资产/超链接/自检），保留 Docling 独立结构化 `engine_document`；原 pages 数组暂留兼容，不退役 B 的旧业务链路。
- 自动持久化到既有 `map/<hash前2>/<hash>.md`，旁边落 `.document_map.json/.assets.json/.hyperlinks.json/.self_check.json/.engine.json`。保真告警进入 parse_summary.quality_status/warnings；转换器失败为 parse_failed，不静默成功。
- 自测捕获并修复地图行号缺陷：段落/对象的嵌入换行原先只按一个列表项计数；现在按真实 Markdown 行数生成区间，补专门回归。已通过的人眼 md 文本未变。
- 真实原件经隔离 venv 的 Docling 2.130.0 转换实测 parsed，独立结构化引擎结果非空；18 表、6 对象；实际 map：`/tmp/sam-m5/a2-check/2a/map/65/6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965.md`。
- 输入 SHA256 保持 `6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965`；只写新建隔离子目录，不更新用户已有库，不改/删真实原件。
- 合格源映射无公司人工更正时直接读新 md；已有人工更正覆盖的旧视图兼容路径暂保留，不擅自重构更正语义。
- 配套 A2 工作区全量已测到 925 passed / 1 skipped（115.51s），最后字体修正后的最终复跑结果另记于 A2 报告；本接线不宣称 B/C/D/E 已完成。

---

# M5 架构返工：A 阶段 DOCX 独立预览（2026-10-13）

## 状态与边界

**本提交是 A 的独立预览子步骤，不是 A/M5 完工交付。** 先让饭团看到实际渲染产物，再继续解析器接线与后续阶段。没有切换主链路，没有删碎块代码，没有改全局 codeMode 或其断言测试。只本地提交，不 push。

提交阻塞：`git commit -m 'feat(m5): add standalone DOCX fidelity preview for stage A'` 报 `fatal: Unable to create '/home/jakki/projects/startup-ai-manager/.git/index.lock': Read-only file system`。本会话 `.git` 为只读；不绕过、不重试。**没有新 commit**，HEAD 仍为 `da10e78a95616da8d5d479b295eb85706b5b09e6`。四文件已暂存，提交失败后的本报告补充尚未暂存；工作区不能称为干净。

本轮新增三文件：
- `skills/document-ingest/scripts/docx_fidelity.py`：纯格式投影；正文自然段、基于原件样式的标题、原件顺序、真 Markdown 表、网格横纵合并标记、可见坐标、图片/嵌入对象原位链接、地图、资产与超链接清单、自检。
- `skills/document-ingest/scripts/render_docx.py`：明确要求输出目录的旁路预览命令；不连接数据库、不派员工、不调用模型。质量告警 exit 2，仍保留可检查产物。
- `tests/test_m5_docx_fidelity.py`：11 个确定性回归用例，包括财务表各列、合并格、继承标题、拆 run 数字、同一 run 内图片顺序、特殊格式回链、漏对象告警、哈希不符拒绝、CLI 与原件不变。
- 本报告在旧报告前追加本轮记录，旧证据不覆盖。

## 隔离原件实测（格式验证，不是员工事实抽取验收）

只读输入：`/tmp/sam-m5/material/fmt_C_info.docx`；实际 SHA256：`6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965`。

保留给人眼验收的预览：`/tmp/sam-m5/a-preview-v1/document.md`，401 行 / 24019 bytes。同目录含 `document_map.json`、`assets.json`、`hyperlinks.json`、`self_check.json` 与对象 bytes。

复现：
```bash
.venv/bin/python skills/document-ingest/scripts/render_docx.py \
  /tmp/sam-m5/material/fmt_C_info.docx \
  --output-dir /tmp/sam-m5/a-preview-v1
```
该真实文件返回 exit 2（下列显式告警），不宣称质量 pass。

- 原件 18 张表 → 正文 18 张表；逐表行数/列网格与渲染行列一致，详见 `self_check.json.table_dimensions`。
- 原件 ZIP 实际是 **5 个媒体文件 + 1 个嵌入 Word 对象**，不是总共 5 个对象。抽取/原位链接/清单均为 **6**；不能为了迎合旧基线漏掉嵌入对象。
- 财务表为 `docx:body/99/table`，7 行 × 4 列、无合并。三年营收、研发费、研发占比及人员数据完整保留，未进行业务判断；正文 14.86% 原样保留。
- 正文可见字符多重集覆盖率 100%，连续八字片段覆盖率 100%。**这是本投影的正文口径，不等于旧 Docling 的中文片段指标，也不包括页脚/视觉版式**，不可直接拿它替换整份原件覆盖验收。
- `.emf`、嵌入 Word 没有生成快照：保留原对象并链接原件，明确“需回看原件/视觉处理、页码未知”；页脚未纳入正文投影也单列告警。
- 未启动任何模型；模型调用为 0 仅表示纯格式预览，不是 B 新链路调用量实测，未给预算默认值。

## 测试

- 新增专项：11 passed（0.28s）。
- 原始环境全量：895 passed / 3 skipped / 1 failed（66.29s）。失败：`tests/test_runtime_bootstrap.py::test_prepare_runtime_mounts_plugin_and_writes_fresh_9_6_shape`。
- 完整子进程报错：`OpenClaw Gateway startup blocked on port 18790: OpenClaw Gateway port is occupied by an unverified process`。没有杀现有进程，没有改测试绕过 codeMode 断言。
- 空闲端口 `SAM_OPENCLAW_GATEWAY_PORT=48067` 复跑（当时已收集 9 个新增用例）：896 passed / 3 skipped（69.19s）。
- 添加最后两个边界用例后的最终全量（同一空闲测试端口）：**898 passed / 3 skipped / 0 failed（62.29s）**。
- `.venv/bin/python -m compileall -q app scripts skills/document-ingest/scripts` exit 0。
- `git diff --check` exit 0；交付前重读新增代码与测试。

## 未完成项（不能放行 M5）

1. A 尚未将新产物接入 ingestion manifest / 持久化地图 / job 质量状态；原来的 `map/<hash>.md` 与主链路完全未动。PDF/Excel/PPT 通用投影未施工。当前 sam_file_get 仅收 file_hash 元数据，**sam://object URI 还未接到员工工具/HTTP 取图**，预览 URI 是待接通契约，不宣称可点击已闭环。
2. 特殊对象的原件高清页面快照、页码定位未实现；表内对象现以格坐标回链并显式告警，复杂嵌套表/文本框版式仍需质检核验。未定位对象会告警，当前不伪造其出现位置。
3. A2 质检员工、bwrap 受控通道、独立 venv 固定版本、安全实测和重转门均未施工。
4. B/C/D/E 均未施工；事实抽取、投影坏条隔离、分阶段真实调用统计、保留成果/续跑、长文档验收未实现。不宣称财务事实已入库或 job done。
5. 本轮程序无数据库连接；预览只写新增 `/tmp/sam-m5/a-preview-v1`，未更改/删除原件，也未清理用户原有隔离库。已删除本轮早期临时预览与 bootstrap-check 目录；最终预览保留供饭团验收，之后再按任务回收。

---

# SAM M5 第二轮 P1 交付报告

## 提交边界与工作区卫生

- 分支保持 `feat/unified-prototype-ui`，只做本地 commit，不 push。
- ① `a48f051` — `fix(m5): P1 韧性修复归档`。
  - 审核原有 7 文件：员工输出/备用配置、空/截断响应重试、skill 输出约束、分类失败不丢已提交 L2、对应回归测试；没有新增 UI 或 module-projection 实现。
  - 额外同步两个必要配套文件：`harness-openclaw/skill-registry/catalog.json`（skill 版本/摘要），`tests/test_finance_worker.py`（空传输响应应是 EmployeeTransportError，不再是 WorkerUnavailable）。共 9 文件。
  - 初始全量：849 passed / 3 skipped / 3 failed。完整失败原因：空响应异常类型断言过期；catalog 与 skill 不一致；bootstrap 报 `OpenClaw Gateway startup blocked on port 18790: OpenClaw Gateway port is occupied by an unverified process`。
  - 没有杀现有网关。隔离端口 28791 复跑：**852 passed / 3 skipped**；compileall、diff-check 均 exit 0。
- ② 本报告所在的后续独立提交 — `fix(m5): batch attribution and bounded employee retries`。提交 hash 见交付回执；不把报告自身 hash 写入自身。
- 没有对 `data/` 主库执行迁移、写入或真实材料模型复验。主库 mtime 仍为开工前的 2026-10-12 11:25；最终全量前后 `data/app.db` SHA-256 相同。隔离复验库/原件/日志只读，没有清理或改写用户现存 `/tmp/sam-m5/` 数据。

## 本轮文件清单

| 文件 | 内容 |
| --- | --- |
| `app/runtime_errors.py` | 传输重试矩阵、确定性错误优先、保留 stopReason/HTTP 元数据、凭据脱敏、1200 字符错误摘要 |
| `app/employees/worker.py` | 统一重试、最后根因优先、尝试数、单请求模型工具模式、备用尝试、预算注入 |
| `app/employees/model_tool.py`（新） | 加载员工 skill 的无历史单请求工具；HTTP 根因；进程级硬时限；请求通过 stdin，不放 CLI 参数或临时文件 |
| `app/employees/budget.py`（新） | 归属预算、解析后处理共享预算、ContextVar 隔离、不可重试熔断异常 |
| `app/employees/semantic.py` | 批归属协议；严格校验 block_id 覆盖/唯一性与既有归属字段 |
| `app/entities/bridge.py` | 数量/字符双界限分批；结构语境；失败前缀保留；计数持久化/日志 |
| `app/employee_policy.py` | 解析总时限默认由 3600s 降为 900s |
| `app/storage/mapping_store.py` | 行坐标兼容，不把所有行混成同一个表坐标 |
| `webapp/app.py` | 任务预算作用域、后处理员工单请求模式、终态计数持久化/日志；没有 UI 修改 |
| `skills/document-classifier/SKILL.md` | 员工批归属规则、逐 ID 返回、结构语境与证据区分、无历史不猜测 |
| `harness-openclaw/skill-registry/catalog.json` | 同步上述 skill 版本/摘要 |
| `skills/document-ingest/scripts/bridge` | Docling 转换后的 Word OOXML 结构归一化，避免单元格段落重复进入正文块 |
| `skills/document-ingest/schemas/l2-manifest.schema.json` | 可选结构顺序 `order`、逐行出处 `row_source_locs` |
| `tests/test_m5_attribution_batches.py`（新） | 35 项新测试，见下 |
| `tests/test_2b_unresolved_handoff.py` | 既有测试桩增加批协议所需 block_id，保留原有断言 |
| `tests/test_employee_resilience.py` | 错误摘要上限断言从 200 更新为 1200，以保存根因 |
| `report.md`（新） | 本交付报告 |

后端没有新增实体关键词、业务正则、别名、模块映射或业务归类枚举。归属标签沿用原有参数 schema。新增正则仅检查传输错误/脱敏；XML 标签仅用于文档结构。判断仍由模型读取 skill、通过模型工具作出；宿主不按文字归类。

## A：重试分类矩阵

| 传输结果 | 重试 | 说明 |
| --- | --- | --- |
| `Agent couldn't generate a response. Please try again.`（含弯引号） | 是 | 错误信封/通道异常才检查，不检查成功业务文本 |
| 超时、HTTP 408 | 是 | 每次与任务总 deadline 同时限制 |
| HTTP 429、限流 | 是 | 有限退避，不无限等待 |
| HTTP 5xx | 是 | 原始 HTTP 码/错误原因保留 |
| 连接 reset/refused/aborted/closed、RemoteDisconnected、IncompleteRead、TLS EOF、临时 DNS 错误 | 是 | TLS 证书配置错误不在 TLS EOF 分类内 |
| 空通道结果、空 payloads、stopReason=length | 是 | 显式 `candidates: []` 仍是员工业务结果，不重试 |
| HTTP 4xx（除 408/429）、鉴权失败、确定性参数/schema 错误 | 否 | 即使同时有 empty/try again，也不盲目重试 |
| 有效 ambiguous / needs_review / unresolved | 否 | 不借重试改变判断 |
| 预算熔断 | 否 | `error_kind=employee_budget`，不切备用继续烧钱 |

- 默认主模型最多 **3 次**（原有重试上限 2），指数退避 **1s、2s**，单次退避上限 4s。已有环境变量仍有效。
- 单请求工具配置 `SAM_EMPLOYEE_FALLBACK_MODEL` 后，主模型可重试传输错误耗尽，且剩余预算允许时，再给备用模型 **1 次**机会；401/schema 错误不切备用。备用请求也计入两个预算。
- 单请求工具的备用模型使用同一已配置 endpoint，支持裸模型名或 `token-plan/<model>`；其他 provider 前缀明确拒绝，不猜测端点/凭据。既有非解析 OpenClaw 通路保留网关自己的 fallback 配置。
- 终态错误包含 `attempts=N`、最后根因（优先于 earlier 历史）、已提供的 stopReason/HTTP 码；HTTP 参数错误读取最多 4096 bytes 响应诊断并脱敏。摘要限 1200 字符。不凭空补网关未提供的元数据。

## B：批处理、结构聚合、短调用

### 批协议与复杂度

- 默认每批最多 **32 块 / 24000 源文与结构语境字符**，任一界限先到即分批。
- 模型一次接收 `{company_name, aliases, blocks:[{block_id,text,context}]}`，一次返回多个 candidates；按显式 block_id 对齐，允许响应重排，不按位置猜配。
- 缺失、重复、越界、bool 冒充 integer ID 或无效归属 schema：该批显式 ambiguous/needs_review，不悄悄归 self。
- 无历史的单请求模型工具：system=现有 skill，user=当前批；不挂 embedded catalog，不触发 agent 里的多次 tool/model 循环，不复用 `parse-job:<id>` 会话。
- **53 块测试实测 2 次请求，批大小 [32,21]**。归属模型调用正常路径为 O(批次数)，不是 O(N)。这不声称财务抽取也已批量化：财务抽取仍沿用已有逐块契约，但同样进入任务预算、无历史单请求模式。
- 超大单块不会截断正文/猜结论：超过字符界限即明确熔断，保留已完成块。

### Word 表格与出处

- Docling 转换后，根据 Word XML 的真实 body/table/row/cell 边界归一化：正文不再重复包含表内单元格段落；一个物理行合成一块，保留全部单元格、显式表头及原文数值。
- 不猜测“第一行一定是表头”；只用 Word `tblHeader` 元数据标记表头。所有物理行（含表头）均保留。第一物理行还可以作为独立 `context` 发送给归属员工，但不写入其他行正文，也不进入这些行的事实抽取证据。
- 内容控件/包装节点递归保留；tab、换行保留；用结构 order 恢复正文与表格原有顺序。
- 保留 `docx:body/.../table/row/<index>` 出处，不伪造 Word 页码。schema 与 L2 mapping 接受逐行出处。

### 预算默认值与熔断

| 环境变量 | 默认值 | 范围 |
| --- | ---: | --- |
| `SAM_ATTRIBUTION_BATCH_SIZE` | 32 | 归属批块数 |
| `SAM_ATTRIBUTION_BATCH_CHARS` | 24000 | 当前批源文/结构语境字符 |
| `SAM_ATTRIBUTION_MAX_MODEL_CALLS` | 12 | 同文件本轮归属的模型工具尝试总数，含重试/备用 |
| `SAM_ATTRIBUTION_TOTAL_TIMEOUT` | 600s | 同文件归属总时限 |
| `SAM_PARSE_MAX_MODEL_CALLS` | 64 | 任务内所有**解析后处理员工**模型工具尝试数 |
| `SAM_PARSE_TOTAL_TIMEOUT` | 900s | 整个 worker 时限，含解析入口消耗的时间；原默认 3600s |
| `SAM_EMPLOYEE_MAX_OUTPUT_TOKENS` | 16384 | 单请求输出限制（上一轮已有参数，继续使用） |

- 归属与任务预算共享计数的约束同时生效。不同任务/公司的 ContextVar 隔离，作用域结束 reset。
- 单请求进程 timeout=min(员工单次时限、员工总时限、任务剩余、归属剩余)，不是仅靠 socket read timeout。子进程超时被终止，不会因不断滴流而无限占用本地 worker。
- 预算不足不发下一次请求。熔断/传输耗尽时，将已经完成的块和原坐标写入独立 artifact；artifact=failed，保留完成前缀，不把任务伪装成 completed，也不把尚未判定的块猜成 self。已提交 L2 仍走原有幂等重试路径，不删除。
- 错误带 artifact_id、completed_blocks、model_calls、batches；`attribution_metrics` 保存块数、完成数、批大小列表、调用尝试数、耗时、宿主/thread/run 标识。`parse_model_metrics` 保存 job/company、后处理调用数、预算、耗时及明确统计范围；日志同样记录。
- **未闭合的范围：解析入口仍是 OpenClaw 的 sam_document_ingest agent。它内部的 model-fetch 往返没有逐调用计数/熔断。** 已有解析单次最多 600s，并受任务剩余时限约束；但 64 次预算与 `model_call_scope=post_parse_employees` 不能冒充整个网关/全解析任务的精确模型调用预算。B4/B5 的全链路口径尚未完全闭合，需进一步接网关逐调用护栏；本轮没有靠读全局日志近似计数或误杀共享网关。

## 隔离复验日志核对（只读）

找到 `/tmp/sam-m5/run/oc-state/gateway.log` 及其 `agents/sam-leader/agent/openclaw-agent.sqlite`，并只读查对应隔离 parse_jobs/staging；没有运行该材料的模型请求。

- 对应 job `j_1791806544427_159822bde6`：failed / employee_transport / l2_committed=0，终态确有 `attempts=1` 与通用无响应文案。
- 同一归属 session 时间 **2026-10-12 20:02:54.998→20:23:19.134 +08:00**，266 transcript events、131 user + 131 assistant，131 个 user 均含逐块归属 TASK；130 个 assistant stop，最后 1 个 length。
- usage 的 input+cacheRead+cacheWrite 从首个 **6792** 到末个 **195377** tokens。末个 output=17 / reasoningTokens=17。
- gateway.log **7632** 行明确记录该 run 的 stopReason=length / payloads=0；**7633–7634** 行再转成 generic no response / fallback next=none。`stdout.new.log` 有最终 attempts=1。
- 因此“长会话持续膨胀”有直接证据，且终态确是截断被通用错误掩盖；不能仅凭同时出现就断言上下文膨胀是唯一原因。新模式无历史，消除该累积路径。
- **口径差异**：594 是该 gateway.log 在失败点前的累计 response 数；按上述会话时间窗口统计是 **131 条**。现存对应 staging 有 **147 text_items + 115 table rows = 262 块**，不是施工单的 53。不能把日志累计数当作该任务独占调用数，或把不同解析结果当成同一批次。
- 对 **621528 bytes** 的隔离归档副本只运行本地 OOXML 结构读取：72 正文段落、18 结构表、133 物理行（含保留表头）=205 块；默认分批计划 **[32,32,32,32,32,32,13]**，无超大块。**这是本地编排结果，不是 7 次真实模型验收通过。**

## 测试与自审

新增 35 项：53 块→2 请求、重排 ID、ID 缺失/重复/越界/bool、字符界限、超大块、次数/时长熔断保留前缀、跨 skill 共享预算/reset、通用 RuntimeError/错误信封重试、HTTP 文本/结构码矩阵、备用触发与401不触发、单 HTTP/无历史/无 tools、子进程硬 deadline/私密 stdin、HTTP 参数根因与 JSON 凭据脱敏、空响应、连接中断/TLS EOF、Word 行/表头/坐标及真实 Docling 合成 docx 集成。

本轮首跑全量 17 failed / 852 passed / 3 skipped：16 项旧归属测试桩缺少新批协议的 block_id，1 项 catalog 未同步。只更新测试桩与生成目录，没有在生产中加入按位置配对兼容逻辑，没有删除原有断言。自审补齐 HTTP 流中断与空通道错误边界后复跑。

最终命令与结果见下（报告写入后再检查 whitespace）：

- `SAM_OPENCLAW_GATEWAY_PORT=28791 .venv/bin/python -m pytest -q`：**887 passed / 3 skipped，66.24s**。
- `.venv/bin/python -m pytest -q tests/test_m5_attribution_batches.py`：**35 passed**。
- `.venv/bin/python -m compileall -q app scripts webapp`：exit 0。
- `.venv/bin/python -m py_compile skills/document-ingest/scripts/bridge`：exit 0。
- `git diff --check`：exit 0。
- `sha256sum -c /tmp/m5-main-db-before.sha`：`data/app.db: OK`。

M4 的 818/3 是较早基线；当前分支叠加已有重构/测试后，首提交归档基线实际为 852/3。本轮新增 35 项，没有减少测试、没有新增 skip。

## 仍需真机验收 / 未完全完成项

1. 用隔离副本/acc-* 公司，核对批请求实际 HTTP 数、duration、post_parse_employees 计数与 gateway 解析入口计数分别统计。不得将编排计划当成模型执行结果。
2. 验证 Token Plan 当前模型对 JSON candidates/block_id、多块语境、`max_tokens` 与关闭 thinking 的实际支持；本轮没有消费真实模型额度。
3. 核验真实复杂 Word 的合并单元格、嵌套表、内容控件及列对齐；无依据归属必须维持 ambiguous，而不是通过后端补业务规则。
4. 注入 429/5xx/length/401，核验真实备用模型、最多 3+1 尝试和根因回执；压低预算，确认 failed artifact 的前缀可查、已提交事实不被删。
5. **解析入口网关内部逐调用预算/精确计数仍待实现，B4/B5 不能标为全链路完成。** 本轮解决了归属长会话及解析后处理员工放大路径，但没有宣称整个 M5/Gate/断网验收完成。
6. 不 push。等待饭团真机验收、范范点头；也不触碰既有真实底稿或主库。
