# Handoff · 阶段3a：OV「座位替换」机制只读探针

- 阶段：记忆层重组 阶段3a（探针，先于 3b 落地）
- 依据：`docs/architecture/06-2b-l1-l0-design.md` v2 第 4 节、《SAM×OV对接架构研究.md》
- 性质：**只读 / 临时根实验。禁止改任何业务代码、禁止改 OV 源码、禁止 git commit/push。**
- 产出：本目录 `2026-10-11-ov-seat-probe-findings.md`（逐条结论 + 实际命令 + 原文证据），结论直接决定 3b 实现路径。

## 背景一句话

SAM 要把员工写的 L0/L1 Markdown 放进 OV 原生座位 `.abstract.md` / `.overview.md`，从而白嫖语义检索与分层装配；同时彻底不依赖 OV 内置 VLM（qwen3.8-flash，近期不稳定）。部分机制已从源码读到结论，但有 4 点必须在真机钉死。

## 环境

- 常驻 OV：`http://127.0.0.1:1933`（dev auth），配置 `~/.sam-openviking/ov.conf`，根 `viking://resources/sam`。
- CLI：`ov`（uv tool，python3.14），包目录：
  `/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/`
- SAM venv：`/home/jakki/projects/startup-ai-manager/.venv`。
- 所有实验只用临时根：`viking://resources/sam/_probe/`，结束必须删净并核验（`ov ls`/`ov find` 均无残留）。不要碰 `sam/2b`、`sam/acceptance` 等现有路径。

## 已从源码确认（实验中顺手复核即可，不用重复证明）

- `processing_mode=vectors_only`：不跑语义摘要、仍建向量；`utils/resource_processor.py` 1138/1156 行用 `not vectors_only` 挡住父目录刷新。
- `ov mkdir <dir> --description <文本>`：零 VLM 直接生成 `.abstract.md`（`service/fs_service.py` 380-430，`generated_by=FSService/mkdir`），并 embed。
- 公共 content/write 禁止凭空创建 sidecar，只能在已存在时替换正文；替换后用新正文重新 embed、不调 VLM。
- VLM 不可用时 overview 生成回退占位 `[Directory overview is not ready]`（`storage/queuefs/semantic_processor.py` 1521-1523）。

## 必答问题

### P1（最关键）：如何不调用 flash，为「任意文件夹」生成我们自己的 `.overview.md` 座位？

mkdir 只建 abstract、不建 overview。用实验确定走 A 还是 B，或发现更优解：

- **候选 A（首选）**：找到与 mkdir 内部 `write_file` 写 sidecar 同等的**正规可用入口**，能直接带我们的正文创建 `.overview.md`（例如某内部/高级 API、特定 write 上下文、专用命令）。给出确切调用方式与权限要求；评估 SAM 后端从外部调用是否可行、是否属于受支持接口（避免依赖私有内部实现）。
- **候选 B**：在「VLM 不可用/关闭」前提下触发一次该目录的语义刷新，让它落 `[Directory overview is not ready]` 占位 overview（不调 flash），随后用允许的替换路径把正文换成员工内容。给出：如何稳定让 VLM 不可用（配置项/环境变量，仅临时）、触发刷新的确切命令、占位是否必然生成、替换命令、替换后 embed 是否生效。
- 记录每条路的实际命令、耗时、返回、`ov abstract`/`ov overview` 读取结果、frontmatter 变化。

### P2：vectors_only 写入的普通 md，能否被分层装配器当 L2 正常下钻？

- 在临时根写 `F1.md`（`ov write --processing-mode vectors_only`，**不带 --wait**），mkdir 建带 description 的目录。
- 验证：文件是否被向量索引（`ov find` 命中正文）；读取/检索/分层装配是否把它当 L2 内容返回；没有单文件 `.abstract.md` 是否影响下钻与 token 装配。
- 给出"事实卡这样存，经理能否下钻到单条"的明确结论。

### P3：员工座位内容的检索与三级下钻链路

建 `_probe/<company>/06-销售/`，分别放：模块 abstract/overview（用 P1 结论的方法）、公司根 abstract/overview（mkdir description + P1）、2-3 张事实卡。验证：

- `ov find "<事实里的特征短语>"` 命中的是我们写的座位/事实卡，而非 flash 内容；
- `ov overview`/`ov abstract` 返回员工正文；
- 公司根 → 模块 → 事实卡的逐级展开是否顺畅（必要时用 retrieve/context 相关命令或最小 python 调用装配器验证）；
- 观察 `ov ls` agent 输出里 abstract/overview 长度截断行为。

### P4：URI 公司段重复 bug 定位（只读，不改）

现状代码曾生成 `.../2b/<company>/<company>/L1/brief.json`（公司段重复，曾致 NOT_FOUND）。在 `app/memory/company_facts/paths.py` 及调用方定位重复拼接的具体位置，报告文件:行号与当前拼接逻辑，作为 3b 修复输入，**本次不要改**。

## 约束

- 不写业务代码、不动 OV 源码、不提交；实验脚本可放 `/tmp` 或 `.runtime/`（gitignored），不要落进仓库 tracked 路径。
- 实验全程不得依赖 flash 成功；如某步必须 VLM，明确标注并改用「VLM 不可用」前提重试。
- 结束：删净 `_probe/`，用 `ov ls` 与 `ov find probe`（特征词）双重确认无残留；报告里附清理核验输出。
- 如发现与上述"源码已确认"不符，以真机为准，明确写出差异。

## 报告格式（findings 文档）

每个 Px：结论（能/不能/条件能）→ 复现命令 → 关键输出/证据 → 对 3b 的具体建议（用什么命令/接口、风险点）。末尾给"3b 推荐落地方案"一段。
