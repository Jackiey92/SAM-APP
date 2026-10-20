# 事实树 · 段Q1（真机叶子落地修复）交接档

> 承接段P（`ac88da6`，真机投影通道已通）。B3c 用真实模型 + 鸿通晟 docx 跑通五相（classify 1 / attribute 1 / extract 9 / plan 1 / leaf 14；SQLite 落 151 条事实；门开不再投影原子卡，`m1` 投影行=0；质检 pass、原件零改动），但 **14 片叶子 100% 构建失败（built=0）**。本棒只修"让合规叶子能落地"，不碰已通过的投影通道、不碰数字护栏。冲突成件单独放段Q2。

- 唯一入口 `crisp`，UTF-8 中文 prompt 文件；遇口径/签名冲突即停手列点。
- 真机产物在 `/tmp/sam-b3c-real-1nwosf6g/`（`reader.db` 的 `memory_kv` state、`model-trace/*.response.json` 真模型叶子输出），可用于复现，但属临时目录、不要改它。
- 硬边界：不改数字 union 护栏（`numeric_guard`/`check_union`）、不放松坐标必须真实命中（`quote_resolver`）、不动段P provider、不接 UI、不调真模型、不 push/部署、不写业务正则关键词。默认 `SAM_FACTS_TREE=False`，关闭态逐字节/逐行为不变。

## 0. 14 片失败的四类根因（已对真机输出+真实代码逐条核实）

state：`leaves_failed` 12 条 `paragraph lacks terminal footnote` + 2 条 `footnote source not declared or ambiguous`。逐条核对 14 份真模型 markdown 后分四类：

### A. 句末标点在脚注之后（主因，约 10 片，纯后端语法护栏过严）
真模型中文写作：`…占营业收入的5.49%[^1]。`、`…解决客户痛点 [^1]。`（脚注后还可能有一个空格再句号）。
现护栏 `app/facts_tree/leaf_store.py:300` 要求脚注在段最末（仅允许尾部空白）：
```python
re.search(r'(?:\[\^[^\]\s]+\])+\s*$', paragraph)
```
冲突件同款规则在 `:350`。
**这是 Markdown 语法规则不是业务规则**，且不削弱"每句陈述必须带出处"：允许**脚注标记之后只跟空白与句末点号**（中文 `。！？；：、）】」…`、英文 `.!?;:`、收尾右括号/引号），但不允许脚注后再出现任何实质性文字（仍防"一段只在中间挂脚注、末尾裸结论"）。
- 受影响叶子实测尾部示例（证明放宽后全部命中）：`盈利模式[^1]。`、`合理利润[^1]。`、`下浮一定比例[^1]。`、`合计82人[^2]。`、`超1亿元[^1]。`、`互利共赢[^2]。`、`三大产品线 [^1]。`（带空格）。
- 把 `:300` 与 `:350` 的"段末脚注"判定抽成一个共用纯函数（如 `_ends_with_footnote(paragraph)`），两处同规则；正则允许尾部 `[空白]*(?:[句末点号/收尾括号引号][空白]*)*$`，但脚注后若还有字母/数字/汉字等实质 token 仍必须判失败。**保留并新增测试**：脚注后再接一句无出处的话（如 `…[^1]。这是无出处结论`）必须仍被拒。

### B. 引导表格的冒号句本身无脚注（2 片：L-bizov-15、L-hr-01，模型问题，技能收紧即可，后端不改）
尾部 `…以及域名注册情况。具体明细如下：` 后紧跟 markdown 表格，被段末脚注检查判失败。
这是员工写法问题（表格的脚注只在表行，引导句裸奔）。**技能层要求**：以冒号收束、引出表格/列表的引导句，必须在冒号前带上它所引材料的脚注（如 `…主要任职人员如下[^1]：` 或在句末标点规则允许的位置）。本棒后端**不**为此开口子（避免放过无出处引导句）；只在 SKILL 明确写法并给正反例。真机复测若个别仍失败，属模型质量，下一轮再评估是否需要结构化豁免。

### C. 无内容占位叶（1 片：L-info-12，组织架构，模型无证据却硬写）
真模型正文：`本叶子暂无法整理出确定的组织架构与职责分工事实。`（无脚注、无事实），被段末脚注护栏正确拒绝。**这个拒绝要保留**，但不应记成"failed leaf"（不是构建失败，是"该叶无内容"）。
- 技能层明确：没有可引用证据时，plan 就不要选该叶；leaf 阶段若确无内容，返回明确空标记而不是占位段落（见 §1 输出契约）。
- 编排层 `app/facts_tree/leaf_plan.py:build_one` 识别该空标记：不计入 `leaves_built`、也不计入 `leaves_failed`，计入 state 的一个独立 `leaves_empty`（结构字段，非业务分类），不 rollup、不写树。加对应假员工测试（无内容→empty，零写入；有内容→built）。

### D. 脚注原件名拼错/被截断（数片：L-info-03/04/11 等，模型问题，技能+宿主提示修复）
真模型脚注定义里写了 `mt_C_info.docx`、`fo.docx`、甚至 `ph; fmt_C_info.docx`，真实原件名是 `fmt_C_info.docx`，触发 `:259 footnote source not declared or ambiguous`。
编排器 `_fragments` 其实已给每个片段带 `original_name`（`leaf_plan.py:31`），但模型仍臆造。两处修复：
1. **宿主**：leaf 请求里显式给一个顶层 `declared_sources`（数组，元素含 `file_hash` 与 `original_name`，来自本任务实际授权文件），并在技能里强制"脚注原件名必须逐字从这个清单取，禁止缩写/改写/前缀"。
2. **后端报错可读化（纯格式）**：`:259` 拒绝时把"本任务已声明原件名"带进异常信息（如 `footnote source not declared; declared originals: [...]`），便于诊断/未来反馈重试。**不改变拒绝结论**。

## 1. fact-leaf 技能（`skills/fact-leaf/SKILL.md`）必须改清的点

版本号 0.2.0 → 0.3.0，frontmatter 其余字段不动。补充/改写：
1. 脚注位置：每个自然段、每张表的实质性陈述必须带脚注；**脚注标记写在句末点号之前或之后都合法，但其后不得再写任何无脚注的实质内容**（给正例 `…为5.49%[^1]。` 与 `…为5.49。[^1]`；给反例 `…为5.49%[^1]。可见公司很强。`）。脚注标记与前文之间不要加空格。
2. 引出表格/列表的冒号引导句也要带脚注（正例：`主要任职人员如下[^3]：`）。
3. 原件名：脚注定义 `[^n]: 原件名 → 坐标` 中的"原件名"必须**逐字照抄输入 `declared_sources` 里的 original_name**，给反例（真名 `fmt_C_info.docx`，禁止写成 `mt_C_info.docx`/`fo.docx`/`ph; fmt_C_info.docx`）；坐标必须逐字用 fragment 的 `source_loc`。
4. 无内容：plan 阶段没有可引用片段就不要选该叶；leaf 阶段若目标叶确实没有任何可引用证据，返回空标记 `{"leaf_code":"...","empty":true,"note":"..."}`（此时可不写 markdown/脚注），**严禁**输出"暂无法整理/无信息/待补"之类占位段落。
5. 其余既有规则（只选模板叶、不脑补、数字逐字命中、冲突只报告、frontmatter/编辑历史由宿主装配、只返回裸 JSON）保持。

## 2. 输出契约 / 编排器 / catalog

- `leaf_contract.parse_employee_leaf`：在保持现有形状校验的前提下，接受 `empty:true` 的空负载（leaf_code 仍必须为 str 且等于目标；empty 时 markdown/source_files/fact_ids 允许为空/缺省，note 仍为 str）。非 empty 时维持现有严格校验不变。这是形状校验，不含业务判断。
- `leaf_plan.build_one`：empty → 记 `leaves_empty`、返回一个明确结构（如 `{'leaf_code':code,'empty':True}`），不落盘不 rollup；非 empty 走原 build_leaf。
- `document_read/service.py:_run_leaves`：state 增加 `leaves_empty` 初始化/断点续跑处理（与 built/failed 同样在 save 前更新，续跑不重复计）；**注意这是主流水线文件，改动必须保持关闭态不 import/不构造编排器（现有门已在 `_run_leaves` 调用点之外），且只在门开路径生效**。
- 宿主 leaf 请求增加 `declared_sources`（在 `leaf_plan.build_one` 的 request dict 里，来源 `self.documents` 的 sha→`store._sources` 得到的 original_name）；plan 请求也可带同一清单。
- 技能改完后用**官方** `scripts/build_phase1_catalog.py` 重新生成 `harness-openclaw/skill-registry/catalog.json`（fact-leaf 版本 0.3.0，技能总数不变=9），`rejected.json` 必须仍为 `[]`；tool-registry 零变化；同步更新写死技能版本/集合的测试（参照 B2a 段做法，只改必要快照断言）。

## 3. 必须新增/更新的测试（离线、假员工/真机 provider，不调真模型）

1. 段末脚注放宽：`[^1]。`、`痛点 [^1]。`、`[^2]！`、英文 `.[^1]` 等标点在脚注前后两种写法都通过；**脚注后再跟无出处实质句仍拒绝**（守住底线）；普通叶与冲突件两处规则都覆盖。
2. 无内容：假员工对某叶返回 `empty:true` → state.leaves_empty 记录、built/failed 不含它、m1_documents/leaf_revisions/leaf_index 零写入、不触发 rollup；该叶断点续跑不重复；有内容叶子正常 built。
3. 原件名：合法真名通过；`mt_C_info.docx`/`fo.docx`/`ph; fmt...` 仍被拒，且错误信息包含 declared originals 清单。
4. 关闭态：`SAM_FACTS_TREE=False` 下 service 行为/state 形状/golden 与现在逐字节一致（不出现 leaves_empty 引起的关闭态差异）；段P 真机 provider 测试、既有 facts_tree/document_read 测试全绿。
5. catalog：fact-leaf 0.3.0、9 技能、rejected=[]、tool-registry 无 diff。
6. 全量只增不减、0 failed/0 error；compileall、git diff --check。

## 4. 交付 / 自证 / 停手点

- 报告 `docs/handoffs/facts-tree-segment-q1-report.md`：改动文件、新增/更新测试名、§0 四类根因各自如何被覆盖、关闭态不变证据、catalog 变化。
- 你沙箱跑不了 pytest（conftest socket），可只做 compileall/diff-check；**全量数字由饭团在正常 shell 复跑**，但你必须把代码改完整、不要留半成品。
- 若发现放宽段末正则会与数字 union 护栏（check_union 依赖段内 FOOTNOTE.findall）相互影响，或 empty 契约会被误当成成功叶，**停手列点**，不要硬改。
- 不 stage harness-openclaw/ 里与本棒无关的既有脏文件（DREAMS.md、dreaming/ 等）；catalog.json/rejected.json 是本棒要求的正规产物，可随本棒改但要在报告说明。遇 index.lock 只读则保留暂存停手，由饭团提交。
做完停在本棒，不自行进入段Q2（冲突成件）。
