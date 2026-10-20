# 事实树 B2a 回执（§0.2/§0.3 裁决已落实）

日期：2026-10-14。基于 HEAD `80afc86`（含 B1 `0cd8e56` 与三次裁决）。
只做 B2a，默认关闭、未接主流水线、未 push/部署；最终验证与提交结果见下文。

## 新增/更新文件

- `app/facts_tree/quote_sources.py`：任务授予净稿的独立快照；只开放 resolve，直接复用 Dossier.read。
  未授权 sha、不存在坐标、非法/越界坐标返回 None；不读 DB、原件或图片对象。
- `app/facts_tree/leaf_contract.py`：五字段形状校验，返回独立列表；bool 不作为整数 fact_id。
  不接模型，不剥 JSON fence，不做语义、不补事实。
- `app/facts_tree/leaf_build.py`：开关先行；复用 B1 _leaf/_sources/_facts/_footnotes；
  所有脚注坐标严格预检；机械补 frontmatter、期间最大值/unspecified 与一行编辑历史。
  公司 projection_lock 覆盖装配与发布，复用其 RLock 防并发版本竞争；不变请求复用旧全文
  让 B1 自己执行内容 sha no-op 和再次核验。审计单元格转义管道/换行/脚注起始字符。
  发布及 fact_leaf_links 全交 B1 同事务，无新增写库 SQL；B1 返回值不变；
  updated_at 查询对应 leaf_revisions.created_at。build_conflict 仅坐标预检与薄透传。
- `skills/fact-leaf/SKILL.md`：任务输入、单 JSON 输出、正文段/表格/脚注及不造数契约，
  员工不写 frontmatter/编辑历史；资料不作为指令，不读全库或原件。
- `tests/test_facts_tree_build.py`：51 项新增测试，全部临时库与假员工/假净稿。
- `tests/test_phase1_catalog.py`：技能集合增加 fact-leaf，保留 catalog==artifact、rejected==[] 严格断言。
- `harness-openclaw/skill-registry/catalog.json`：官方脚本登记第 9 项技能，roles=[]、version=0.1.0。
- `harness-openclaw/skill-registry/rejected.json`：官方脚本重生成，仍为 []，与 HEAD 字节一致，无需产生空提交。
- 本报告。

## 测试与真实结果

1. 首轮新增测试：43 passed / 1 failed。失败为冲突测试夹具概览没有段末脚注：
   `app.facts_tree.leaf_store.LeafRejected: conflict paragraph lacks terminal footnote`。
   修正夹具脚注；未改 B1 行为。
2. B2a + B1 叶子/汇总测试（扩展测试前）：
   `.venv/bin/python -m pytest -q tests/test_facts_tree_build.py tests/test_facts_tree_leaves.py tests/test_facts_tree_rollup.py`
   → **149 passed in 8.86s**。
3. 最终新增测试：`.venv/bin/python -m pytest -q tests/test_facts_tree_build.py`
   → **51 passed in 2.04s**。
4. 第一轮全量（收集时新增用例仍为 44 项）：`.venv/bin/python -m pytest -q`
   → **1261 passed / 1 skipped / 1 failed in 145.92s**。日志 `/tmp/sam-b2a-full.log`。
5. 扩展至 51 项后的全量已经启动；发现第一轮全量目录快照冲突后主动中断，避免无意义重跑。
   → **1181 passed / 1 skipped，KeyboardInterrupt，88.89s**，不是完整全量通过证据。
   日志 `/tmp/sam-b2a-full-final.log`。
6. `.venv/bin/python -m compileall -q app scripts`：exit 0。
7. `git diff --check`：exit 0；新增文件另做 no-index whitespace check。
8. `grep -nE '纳米|涂膜|前五大' app/facts_tree/*.py`：零匹配。
   generic 模板数据既有 scope 中含“前五大”，是既有配置，不是后端逻辑，未改动。

## 覆盖定位

- paragraph/table 逐字取块、描述不漏、授权快照、未授权 sha/缺坐标、无整篇/原件 API：
  `test_resolver_exact_blocks_and_grant_snapshot`；越界：`test_resolver_out_of_bounds_returns_none`。
- 对象/缺字段/错类型/空正文/bool fact_id：`test_contract_*`。
- s 叶落盘、模板标题、sources/count、版号/时间、历史追加、链接结果、no-op：
  `test_build_success_noop_update_and_history`。
- 坐标、数字、跨 trunk、公司来源、悬空脚注、目标、员工伪造元数据/历史、缺数据段：
  `test_build_rejections_leave_no_residue`；真实外国公司 sha：`test_existing_foreign_company_source_rejected`；
  文件名歧义：`test_ambiguous_original_name_rejected`。
- 描述脚注与 quote/fact union：`test_narrative_description_coordinate_and_quote_union`、
  `test_description_without_numbers_still_rejects_missing_coordinate`；表格坐标：
  `test_data_table_coordinate_is_checked_even_without_prose_use`。
- 期间最大值/首版时间与兜底：`test_latest_period_mechanical_max_and_first_created_at`、`test_unspecified_period`。
- 冲突路径/target 字段/坐标：`test_conflict_thin_wrapper_and_coordinate_gate`。
- link 插入异常、文档发布异常、发布前撤销 fact：`test_publish_failure_rolls_back`。
  更新失败保留旧文档/历史/link：`test_failed_update_preserves_document_history_and_link`。
- no-op 再核验、并发装配：`test_noop_revalidates_authority`、`test_concurrent_host_assembly_is_idempotent`。
- 关闭态 import/build/conflict 零写库、零文档写入：`test_disabled_import_and_build_zero_writes`。
  B1 既有原子卡/模块快照、向量白名单测试保持未改；149 项针对性回归全绿。

## 先前冲突与 §0.3 处理（已解决）

`tests/test_phase1_catalog.py:18` 自动调用 `build_skill_catalog(ROOT / "skills")`，
与 `harness-openclaw/skill-registry/catalog.json` 全量比较；其下一断言还写死原有 8 项技能名单。
新增 `skills/fact-leaf/SKILL.md` 后生成目录必然多出 fact-leaf：

```text
FAILED tests/test_phase1_catalog.py::test_skill_catalog_matches_generated_artifact_and_records_rejections
AssertionError: assert catalog == artifact
Differing items: {'skills': [... fact-leaf ...]} != {'skills': [... 原有技能 ...]}
```

完整 pytest 差异和定位见 `/tmp/sam-b2a-full.log`，原始报错输出未隐藏。

饭团 §0.3 明确允许更新被跟踪的技能契约，不涉及运行时配置。
已按指定模板写 name/description/version=0.1.0/roles=[]/visibility=workspace，执行：

`PYTHONPATH=. .venv/bin/python scripts/build_phase1_catalog.py`

输出 `{"rejected_skills": 0, "rejected_tools": 1, "skills": 9, "tools": 8}`。
唯一 rejected tool 是原有记录，tool-registry manifest/rejected 均零 diff。
更新测试技能集合为 9 项，未放宽 catalog/rejected 断言。
直接调用 build_phase3_role_matrix 比较 roles/phase/runtime/catalog/skill_catalog/tool_search
全部等于既有配置；fact-leaf 不在任何 ROLE_SKILLS；角色配置与 phase3 测试零 diff。
针对性测试 `.venv/bin/python -m pytest -q tests/test_facts_tree_build.py tests/test_phase1_catalog.py tests/test_phase3_contracts.py`
→ **62 passed in 2.12s**。

额外尝试 Codex 通用 skill-creator quick_validate，报
`Unexpected key(s) in SKILL.md frontmatter: version`；该通用检查器不接受 SAM 指定的 version 字段，
不是本项目 SkillRegistry 的验证器。本棒按 §0.3 明确要求保留 version；
SAM 正式生成链接受 fact-leaf，rejected=[]，phase1/phase3 真实测试通过。

## 边界与停手

默认 settings/闸门未改；B1 leaf_store 与所有旧投影/数据库 schema 未改。
没有主流水线调用新增模块，没有运行员工模型/插件/网络服务，没有新增第三方依赖。
除 §0.3 点名的技能生成契约外，既有 harness 脏文件与未跟踪运行文件保持原样，未编辑、未暂存。
本轮新增测试只使用 tmp_path/acc-*，没有写真实公司数据；没有需要清理的真实库测试数据。
停止在 B2a，未进入 B2b/B3；全量与本地提交最终结果附于后节，完成后等待饭团黑盒。

## §0.3 后最终验收

- `.venv/bin/python -m pytest -q`：**1269 passed / 1 skipped / 0 failed in 133.43s (0:02:13)**。
  基线 1218 passed / 1 skipped，新增 51 项，未删除旧用例。
  完整日志 `/tmp/sam-b2a-full-registered.log`，这是本棒最终全量证据；前文失败/中断只作为过程记录。
- `.venv/bin/python -m compileall -q app scripts`：最终 exit 0。
- `git diff --check`：最终 exit 0；所有新增文件另做 no-index whitespace check，零报错。
- tool-registry 的 manifest.json/rejected.json、phase3-roles.json、test_phase3_contracts.py：
  `git diff --stat -- <上述文件>` 无输出；`git diff --exit-code -- <上述文件>` exit 0。
- 技能 rejected.json 与 HEAD 一致且内容为 `[]`，catalog 登记 fact-leaf version=0.1.0、roles=[]。
- 后端 `.py` 业务词 grep 零命中；B1/旧投影/schema/settings/document_read 主流水线零 diff。
- 最终自审已重读三个新增后端模块及契约差异，未发现需要改动既有 B1 行为的点。

## 本地提交阻塞与暂存回执

`git add` 点名本棒文件成功；`git diff --cached --check` exit 0。
暂存 diff 为 **8 files changed, 610 insertions(+), 1 deletion(-)**（写入本节前）。
尝试本地提交：

```text
git commit -m "feat(facts-tree): scoped quote resolver and gated leaf build service (segment B2a)"
fatal: Unable to create '/home/jakki/projects/startup-ai-manager/.git/index.lock': Read-only file system
```

只尝试一次 commit，不重试、不绕过权限。**没有生成 B2a commit hash**；当前 HEAD 仍为
`80afc86a1d191089f2091c39bb309b23d2ea863f`（三次裁决文档提交，不能当成本棒代码提交）。
由饭团在正常 shell 代提交；未 push、未部署。

已暂存本棒文件：

- `app/facts_tree/leaf_build.py`
- `app/facts_tree/leaf_contract.py`
- `app/facts_tree/quote_sources.py`
- `docs/handoffs/facts-tree-segment-b2a-report.md`
- `harness-openclaw/skill-registry/catalog.json`
- `skills/fact-leaf/SKILL.md`
- `tests/test_facts_tree_build.py`
- `tests/test_phase1_catalog.py`

`harness-openclaw/skill-registry/rejected.json` 已在 git add 点名，但内容仍为 []、零 diff，
因此不出现在暂存变更列表；工具清单和 phase3 配置/测试未暂存，无任何差异。
无关 DREAMS.md 与 memory/ 运行时脏文件未进入暂存。
