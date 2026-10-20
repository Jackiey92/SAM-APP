# 段 Q1 — 叶子落地修复

## 状态与范围

起点 ac88da6；实现与静态检查完成，pytest 按派单明确要求未运行（沙箱 conftest socket 受限）。本报告不声称全量绿；全量由饭团正常 shell 复跑。未 stage/commit/push/部署，未调真模型，未进入 Q2。

完整读 18 号交接档。只读打开现场 reader.db（mode=ro），核到 12 个段末脚注失败、2 个原件名失败及 diagnostics 的冲突报告；只读加载 model-trace 下 26 个 response JSON，包括占位叶输出。未修改现场文件。

## 改动文件与四类处置

- app/facts_tree/leaf_store.py：A 抽 `_ends_footnote`，普通叶/冲突件统一允许脚注后的空白与句末点号、收尾括号引号，实质文字仍拒绝。D 原件名拒绝附 `declared originals`，拒绝结论不变。
- app/facts_tree/leaf_contract.py：C 接受明确 boolean empty:true；leaf_code/note 必须字符串，markdown/source_files/fact_ids 只能缺省或空；拒绝携带内容的 empty。非空形状校验保持。
- app/facts_tree/leaf_plan.py：D 每次 leaf 请求从本任务 self.documents + store._sources 构造 declared_sources；不发现新文件。C 在任何构建前核对目标 code，empty 返回独立结构；plan_and_build 独立归集 empty，不进入 built/failed/noop，也不列入 rollup trunks。没有 empty 的既有 wrapper 返回形状不增字段。
- app/document_read/service.py：仅 `_run_leaves` 门开路径初始化 leaves_empty；断点兼容旧 open state，并与 built/failed 一起作为 processed；空负载在 save 前归集，续跑不重复调用或计数。rollup 仅从 built 取 trunks。
- skills/fact-leaf/SKILL.md：0.3.0，A 句末正反例；B 表格/列表引导句必须脚注，后端无豁免；C plan 不选无证据叶、leaf 返回 empty，禁占位；D declared_sources 名称逐字照抄并给真实拼错反例。其它 frontmatter 不变。
- harness-openclaw/skill-registry/catalog.json：官方脚本重生成，仅 fact-leaf 版本和内容 SHA 变化。
- tests/test_facts_tree_q1.py：新增 33 个参数化实例（尚未运行）。
- tests/test_phase1_catalog.py：既有 catalog 测试增加 9 技能与 fact-leaf 0.3.0 断言。
- 本报告。

## 新增测试名称与覆盖

- test_terminal_punctuation_ordinary_and_conflict：10 实例，脚注前后标点、空格及收尾引号括号，普通叶/冲突件两路径。
- test_uncited_tail_and_introduction_still_rejected：6 实例，无出处尾句、裸冒号引导句、占位句，普通叶/冲突件均拒绝且零落盘。
- test_declared_original_exact_name_and_diagnostic：4 实例，真实原件名通过、三种错名拒绝且错误包含声明名称。
- test_empty_contract_rejects_invalid_or_nonempty_payload：6 实例，空标记类型/字段/夹带内容拒绝。
- test_empty_contract_accepts_missing_or_empty_fields：2 实例，缺省或显式空字段接受。
- test_empty_leaf_wrapper_no_writes_or_rollups：独立 empty 归集，built/failed 不含、revisions/index/links 与磁盘零写入、rollup spy 禁调用、请求 declared_sources 正确。
- test_empty_target_mismatch_is_rejection：防止其它目标的 empty 被误当本叶成功。
- test_empty_checkpoint_resume_skips_leaf_and_builds_content：首次 empty 后预算 partial，m1_documents/index/revisions/记忆零写入；重构 reader 续跑只建第二片、有内容正常 built，plan 不重复、empty 不重复、请求清单来自授权原件。
- test_punctuation_does_not_relax_numeric_union：999 不在证据，仍拒绝。
- test_punctuation_does_not_relax_coordinate_gate：不存在坐标仍拒绝。

## 静态自证

- `.venv/bin/python -m compileall -q app scripts tests/test_facts_tree_q1.py tests/test_phase1_catalog.py`：exit 0。
- `git diff --check`：exit 0。
- 官方 `PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py`：exit 0，skills=9、rejected_skills=0。JSON 核对 fact-leaf=0.3.0、catalog.rejected=[]；rejected.json 仍 []、零 diff。
- `git diff --exit-code -- app/memory/provider.py tests/test_document_read.py tests/fixtures/document_read_closed_b2b1.json harness-openclaw/tool-registry`：exit 0。
- numeric_guard/check_union、FOOTNOTE.findall、quote_resolver 不改；段末纯语法 helper 不参与数字提取或证据分配。empty 分支在 builder 前返回，主流水线明确独立分支，不能当 built。
- 关闭态 service 调用门与原分支未动；leaves_empty 只在门开 `_run_leaves` 内出现；关闭态 golden/既有测试原字节未改。行为绿须饭团复跑确认，不能用静态检查代替。

## 工作树与待验收

harness-openclaw/DREAMS.md、dreaming/ 与 session-corpus 等既有脏文件保持原状，不 stage。catalog 是本棒授权产物；tool-registry 无 diff；18 号交接档不加入提交。没有新依赖，没有业务归类规则，没有 provider 或 Q2 冲突成件接线改动。

饭团侧需运行新增测试、既有 facts_tree/document_read/provider 回归与全量 pytest，记录实际 passed/skipped/failed/error 后提交；本沙箱未产生任何 pytest 收尾数字。
