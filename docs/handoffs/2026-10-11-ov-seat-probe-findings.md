# 阶段 3a · OV 座位替换探针 findings

日期：2026-10-11。**本轮未闭环，不构成阶段 3b 放行。**

## 0. 范围、环境与结论总表

| 问题 | 结论 | 3b 影响 |
|---|---|---|
| P1：任意目录无 flash 创建 overview | **未找到当前 dev auth 下已验证的安全创建入口**；write 拒绝创建；cp 创建成功却排队语义刷新；import/export 被鉴权挡住；B 未执行 | 先解决无 VLM 初始化并补探针，不能把 cp 当零-flash方案 |
| P2：vectors_only 事实卡 L2 下钻 | **条件能，真机通过**：level=2 检索、read、显式 full 装配均成功 | 必须显式深化资源 detail，不能假设默认层自动读全文 |
| P3：员工座位检索 / 三级链路 | **部分通过**：L0、已存在 L1 的替换/检索/装配通过；公司→模块→事实卡精确导航通过；完整公司和模块 L1 链路未通过 | 不把部分结果写成整链验收成功 |
| P4：相邻公司段重复定位 | **当前工作树未复现指定形态**；找到 root 契约风险及全部拼接调用链 | 不编造重复行号；需旧错误 URI、运行时 root 或旧版本证据 |

- 仓库分支 `feat/unified-prototype-ui`，起始 HEAD `a6342855bbdfb3f10dea7ab0a9eb5c625145476b`。开始时已有大量未提交改动，含 paths.py、L1/L0 服务及设计 v2；本轮不覆盖这些改动，不提交、不 push。
- 实测 `/health`：`version=0.4.23, auth_mode=dev, healthy=true`。CLI 也是 0.4.23。
- 实际常驻进程：`openviking-server --config /home/jakki/projects/startup-ai-manager/harness-openclaw/state/ov-data/ov.launch.conf --host 127.0.0.1 --port 1933`，**不是交接单写的直接加载 ~/.sam-openviking/ov.conf**。两份磁盘配置均包含 qwen3.8-flash / openai / Token Plan endpoint；未输出 key，未改配置、进程或 OV 源码。磁盘配置不能替代运行时证据。
- 运行时 observer 证实 flash 在用。较早日志中的 `VLM not available` 是历史记录，不能当成本轮 VLM 已禁用。
- 实验写/删除目标均在 `viking://resources/sam/_probe/`；公司为假名 `acc-seat-20261011`。没有直接修改 `sam/2b`、`sam/acceptance`，没有写 SQLite。
- **发现两项不能隐瞒的后端副作用**：cp 触发 flash 语义刷新；最后按交接单删除整个 `_probe` 时，OV 又排队刷新非临时父目录 `sam`。所以本轮不能声称“整个实验零 flash”或“OV 非临时根绝对零副作用”。详见 P1、清理节。
- 已读 `AGENTS.md`、交接单及 `docs/architecture/06-2b-l1-l0-design.md` v2。《SAM×OV对接架构研究.md》未在当前仓库及已检查的本机项目路径找到，未假装读过。

### 公共命令环境

下面所有 `ov` 调用使用 Python subprocess 构造的干净 env：只继承 `PATH/HOME/LANG/LC_ALL/TMPDIR`，再显式注入 SAM 独立 CLI 配置路径；不继承任何 OV_API_KEY* 或代理变量。等价 shell：

```bash
ov_probe() {
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
    OPENVIKING_CLI_CONFIG_FILE="$PWD/harness-openclaw/state/sam-ovcli.conf" \
    ov -o json "$@"
}
# 配置文件内容：{"url": "http://127.0.0.1:1933"}
R=viking://resources/sam/_probe
C="$R/acc-seat-20261011"
M="$C/06-销售"
```

各耗时是客户端调用到回执的墙钟时间，不是异步队列处理总时长；不带 --wait 的 accepted/queued 不等于索引已就绪。

没有运行 pytest / compileall（文档、只读源码分析及临时根机制实验，不是业务代码交付）；不得套用 AGENTS.md 的旧基线为本轮结果。文末有独立 diff 核验。

## P1 · overview 座位的无-flash创建

### 结论

**当前环境不能给出已验证可用于 3b 的零-flash创建方案。** 区分“创建座位”与“替换已有座位”：后一半已证实可行，前一半仍卡住。

### A1：公共 write，不能凭空创建

实际命令：

```bash
ov_probe mkdir "$R/a-empty" --description 'probe 员工导览：紫铜蜂鸟座位实验。'
ov_probe write "$R/a-empty/.overview.md" --mode create --processing-mode vectors_only \
  --content $'# 员工概览\n紫铜蜂鸟销售预测。'
ov_probe write "$R/a-empty/.overview.md" --mode replace --processing-mode vectors_only \
  --content $'# 员工概览\n紫铜蜂鸟销售预测。'
```

create / replace 都返回 `INVALID_ARGUMENT`：`cannot create generated abstract overview directly: .../.overview.md`。无 overview 文件生成。源码 `storage/content_write.py:277-281` 的 batch-write/upsert 也检查 sidecar 已存在，不是另一个创建正门（该项为源码核查，不宣称已跑 batch API）。

### A2：公共 cp，能创建但不能用作零-flash入口

```bash
ov_probe cp "$R/a-empty/.abstract.md" "$R/a-empty/.overview.md"
```

0.090s，成功，`files_created=1`，`vectors.scanned/written=0/0`，**`semantic_root_uri=.../a-empty, semantic_status=queued`**。复制不等于已创建 L1 向量。

马上读取时，overview 是 mkdir 的员工导览；frontmatter `generated_by=FSService/mkdir`，`pending_child_changes=1`。后台处理后，frontmatter 变成 `SemanticProcessor/content_copy`、`pending_child_changes=0`、新增 `missing_summary_entries=0`。observer 的 flash Calls 从首次采样 62 到后续采样 63；该时间窗只有一个本轮 cp 语义任务，和副作用一致。统计是账户级，不是逐请求计费证明；足够排除“cp 完全不调 VLM”的保证。

失败发现后未再用 cp 给公司/模块建座位。只在这一个已存在座位上继续测替换，**没有把 flash 生成正文用作员工数据或验证依赖**。

源码：`service/fs_service.py:736-799`，cp 在 transfer 提交后 `_enqueue_copy_refresh`；没有 processing_mode 参数，不能用 `--processing-mode vectors_only` 关闭它。

### A3：公开 OVPack import/export，接口正规，但当前鉴权不可用

```bash
ov_probe export "$R/a-empty" /tmp/sam-seat-probe/template.ovpack
```

0.219s，`PERMISSION_DENIED`，原文见证据附录：dev auth 不支持该鉴权装饰器要求的 API。未生成本地 pack；没有继续提交未经验证的手造 pack。

源码查到 **正规 HTTP 入口**（不是直接调用私有 write_file）：

1. 上传 `.ovpack` 到 `/api/v1/resources/temp_upload`；
2. `POST /api/v1/pack/import`，body 为 `{temp_file_id, parent, on_conflict, vector_mode}`；
3. CLI 等价 `ov import file.ovpack <parent-uri> --vector-mode recompute`。

`server/routers/pack.py:153-196` 允许 ROOT/ADMIN/USER；但 `server/auth/__init__.py:307-349` 要求存在 api_key_manager，即 **api_key auth + root_api_key 已配置**，dev 模式即便上下文是 ROOT 仍被拒绝；资源 ACL / 范围访问仍应成立。不是“必须给 SAM root 权限”，普通 USER 在 api_key 模式也是路由允许角色。

`storage/ovpack/operations.py:262-384` 允许导入 sidecar，直接持久化后执行 `_enqueue_direct_vectorization`；该函数读取目录 sidecar 并 `vectorize_directory_meta`，无摘要调用。**源码候选，不是本轮已验证路线**。还需要 OVPack v3 格式/校验及现有目录冲突策略验证；`overwrite` 会替换整个导入根，不能当作安全单文件 upsert。建议只作为无 VLM 初始化模板方案的后续候选，不先在 SAM 中绑定私有打包函数。

### A4：add-resource 精确 to，生成的是目录壳，不是座位

```bash
ov_probe add-resource /tmp/sam-seat-probe/seat-overview.md \
  --to "$M/.overview.md" --processing-mode vectors_only
ov_probe read "$M/.overview.md"
ov_probe overview "$M"
```

接收成功（task_id `388cb8d0-20d0-44a8-aba6-af15c0e79ba7`），但 `read` 报 `actual=directory`；`overview` 报 `plugin error: is a directory`。普通 Markdown 导入目录壳即使 to 叫 `.overview.md`，也不变成 sidecar 文件。无 flash 摘要，但破坏这个临时模块的 L1 入口；已随根删除。

WebDAV 也不是替代入口：源码 `server/routers/webdav.py:64-69` / `storage/internal_names.py` 明确拒绝 `.abstract.md/.overview.md`；并且普通 PUT 的 `_write_text_resource` 会 summarize。未为了绕过此护栏发 PUT。

### B：VLM 不可用后刷新，仅源码方案，真机未执行

**本轮没有关掉常驻 VLM，没有执行 semantic reindex，没有获得并替换真实无-VLM占位座位。** 因此不能说 B 已过，也不能说占位必然生成。

- 不能用断网、坏 endpoint、无效 key 冒充 `is_available=False`：`openviking_cli/utils/config/vlm_config.py:831-841` 是配置可用性判断，有 key 时即使 API 报错也仍可用；失败分支不等于 1521 行的占位分支。
- 源码支持的禁用前提：启动配置的整个 `vlm` 对象设为 `{}`，移除 model/key/providers/credentials/backup，并确保没有 account VLM 覆盖；query_planner 不配置，避免检索端依赖另一个模型。`VLMConfig.validate_config` / `_has_any_config` / `_get_effective_api_key` 支持空配置。未发现可作用于一个目录的“关闭 VLM”参数或客户端 env 开关；改 CLI env 不会改变常驻服务的 VLM。
- 后续需饭团批准受控停启 1933、临时配置和恢复计划，或提供已禁用 VLM 的同端口实验环境。本轮提出询问，但没有拿到批准，不自行停启。不要在同一真实数据目录启动第二个 OV。
- **后续待执行命令，不是本轮实跑证据**：

```bash
ov_probe mkdir "$R/b-empty" --description 'probe 员工导览'
# 服务确认实际 VLM unavailable 后：
ov_probe reindex "$R/b-empty" --mode semantic_and_vectors --recursive=false --wait true
ov_probe read "$R/b-empty/.overview.md"  # 必须是文件，正文出现 [Directory overview is not ready]
ov_probe write "$R/b-empty/.overview.md" --processing-mode vectors_only \
  --mode replace --content 'probe 员工自己的简报正文'
ov_probe find '员工自己的简报正文' --uri "$R/b-empty" --level 1 --read-content
```

必须同时测空目录、有 vectors_only 子文件的目录，核对 file/stat、frontmatter、任务终态和 flash Calls 不增长；`ov overview` 返回占位本身不证明文件存在。

### 已有座位替换：能，原文/向量/装配均证实

```bash
ov_probe write "$R/a-empty/.overview.md" --mode replace --processing-mode vectors_only \
  --content $'# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011'
ov_probe read "$R/a-empty/.overview.md"
ov_probe overview "$R/a-empty"
ov_probe find '琥珀水獭员工座位正文' --uri "$R/a-empty" --level 1 --read-content
```

替换 0.057s，`semantic_status=skipped, vector_status=queued`；find 0.085s，命中 `.../a-empty/.overview.md`，`level=1, score=0.906363844871521`，正文为员工文本。读取 frontmatter 保留旧 `SemanticProcessor/content_copy`，**不是 SAM attribution**；应用署名需页脚和 tags。

传只含正文的写入保留受保护 metadata。源码 `storage/abstract_overview.py:347-395` 还说明：round-trip frontmatter 的已知 metadata 若不一致会拒绝；不是“随便加 frontmatter 都会自动丢掉”。本轮只验证正文替换，不声称验证了自定义键写入。

### 与设计 v2 的差异 / 3b 建议

1. vectors_only 普通文件不触发语义刷新：本轮回执与源码一致。
2. mkdir description 生成员工 L0：本轮一致；没有创建 L1。
3. **“已存在非空 sidecar 不会重算”不能推广到普通资源目录**：设计引用 `semantic_processor.py:1809-1816` 实际在 `_skill_root_semantics`。普通 resource 的 cp 已改变 generated_by，证明会重新聚合。rm/mv/cp/reindex 等语义入口仍能覆盖员工座位。
4. `ov overview` 缺文件时会返回**读侧合成**占位；本轮模块实际缺文件时 read=NOT_FOUND，而 overview 可返回 `[Directory overview is not ready]`。这不是 B 所需落盘座位。
5. 3b 不能把 cp、坏 key、单纯 overview 读取当 bootstrap。必须先定并真机验无 VLM 服务/接口路线；还要护住删除、移动、复制等非 write 刷新入口。

## P2 · vectors_only 普通 md 能否作为 L2 下钻

### 结论：能，但默认摘要装配不等于全文

公司和模块用 description 建目录，写入 3 张普通 md，**不带 --wait**。F1 内容原样如下；F2/F3 仅替换 fact_id、标题编号、特征短语、金额、页码和 locator。

```markdown
---
attribution_id: null
fact_id: 1
---
# F1 销售事实卡
probe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。
出处：伪造验收原件 sha256，页码 1，locator probe-row-1。
```

| 卡 | 特征短语 | 金额 | locator |
|---|---|---|---|
| F1 | 极光蓝莓回款 | 12345 | probe-row-1 |
| F2 | 月岩蜜柚合同 | 45678 | probe-row-2 |
| F3 | 青瓷海豚续费 | 23456 | probe-row-3 |

文件以 UTF-8 写到 `/tmp/sam-seat-probe/F{1,2,3}.md`，正文末尾有换行。实际命令见附录；核心为：

```bash
ov_probe mkdir "$C" --description 'probe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。'
ov_probe mkdir "$M" --description 'probe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。'
ov_probe write "$M/F1.md" --from-file /tmp/sam-seat-probe/F1.md --mode create --processing-mode vectors_only
ov_probe find '极光蓝莓回款' --uri "$C" --level 2 --read-content
ov_probe read "$M/F1.md"
ov_probe ls "$M" --all --include-abstract=true --include-overview=true
```

- 写入各耗时 0.048/0.049/0.049s，均 `content_updated=true, semantic_status=skipped, vector_status=queued`。
- 第一次 find 是 INTERNAL，不是成功。服务完整错误链收敛到 `RuntimeError: Local embedding failed: llama_decode returned -1`；一次重试成功（0.085s），F1 `level=2, score=0.8300111293792725`；F2/F3 也返回，内容与写入一致，向量 payload 的 `abstract=""`。
- ls 在导入 overview 目录壳之前列出 `.abstract.md` 和 F1-F3，**没有 `.overview.md` 和单文件摘要文件**。F1 frontmatter 原样保留。
- `ov abstract "$M/F1.md"` 返回**父模块** L0；`ov overview "$M/F1.md"` 返回父模块的读侧缺失占位。不是这张事实卡的独立摘要。事实下钻用 exact `read` 或显式 full 装配。

### 真机原生 context 装配

通过 `POST /api/v1/search/search` 的公开 context 面调用（CLI 的 search help 未暴露该面）。先用 `set-tags` 给 F1 加本轮 tag，避免检索真实公司数据。

```bash
ov_probe set-tags "$M/F1.md" --tags probe_run=seat-probe-20261011
curl --noproxy '*' -sS http://127.0.0.1:1933/api/v1/search/search \
  -H 'Content-Type: application/json' -d '{
    "query":"极光蓝莓回款", "mode":"context", "query_expansion":"off", "rewrite":false,
    "max_tokens":2000,
    "quotas":{"resources":3,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},
    "tags":["probe_run=seat-probe-20261011"], "detail":{"resources":"full"}, "telemetry":true
  }'
```

实际 HTTP 用 Python urllib（禁用代理），请求 body 与上述相同。**不使用 session、expansion 或 rewrite，不触发模型规划/改写**。

| 请求 detail / budget | 返回 detail | 正文 | used_tokens |
|---|---|---|---|
| abstract / 2000 | uri | 空，只给 F1 URI | 33 |
| overview / 2000 | overview | 从 F1 普通正文机械提取标题+首段；含 frontmatter 文本 | 122 |
| full / 2000 | full | F1 完整文本及 attribution_id/出处 | 121 |
| full / 64 | uri | 空，预算不足降级 | 33 |

**经理能精确下钻到单条事实；没有单文件 .abstract 不影响 L2 索引、read 和 full tier。** 但会使默认 abstract 资源层退回 URI，而且资源类别默认不自动提升到全文（`retrieve/context_assembler/params.py:70-102`）。3b 必须有显式 URI read/detail=full 的动作；不能声称“小预算/默认层也能把事实正文自动装进来”。overview tier 的首段机械提取不适合取代精确事实全文。

## P3 · 员工座位检索与三级导航

### 结论：部分能，L1 初始化链路仍未闭环

1. 公司和 `06-销售` 的 L0 都为 description 员工正文；`find '紫铜蜂鸟销售员工导览' --uri "$C" --level 0 --read-content` 命中公司和模块的 `.abstract.md`，score 分别 `0.8918378353118896`、`0.8338383436203003`，frontmatter 为 `FSService/mkdir`。
2. 公司 L0 的模块提示 → `ov ls "$C"` 返回 `06-销售` 及模块 L0 → `ov ls "$M"` 返回 F1-F3 → `ov read "$M/F1.md"` 返回一条事实，**精确导航顺畅**。不是自动递归解析 Markdown 引用，也不是全三级 L1 已验。
3. 单独 `a-empty` 的已有 L1 替换为员工正文，find 命中该员工正文，不是 flash 旧文。`set-tags "$R/a-empty" --tags probe_run=seat-probe-overview` 后，context 请求 detail=overview、max_tokens=2000，返回该目录、员工正文、`used_tokens=72`。原文见附录。
4. **没有给公司根及模块同时建立零-flash .overview.md**：公司缺失 L1；模块尝试 A4 后成错误目录壳。这正是 P1 未闭环导致的 P3 缺项，不伪装成完整通过。

### ls 的 agent 摘要长度，已实测

在已有 `a-empty` 上写了纯员工长文本（vectors_only）。生成办法：

```python
l0 = 'probe 琥珀水獭员工导览：' + '员工编写，只描述临时座位实验。' * 24 + 'END-L0'
l1 = '# 员工概览\nprobe 琥珀水獭员工座位正文。\n' + '员工编写的简报，所有事实均为临时验收虚构内容。' * 220 + 'END-L1'
```

```bash
ov_probe write "$R/a-empty/.abstract.md" --from-file /tmp/sam-seat-probe/long-l0.md --processing-mode vectors_only
ov_probe write "$R/a-empty/.overview.md" --from-file /tmp/sam-seat-probe/long-l1.md --processing-mode vectors_only
ov_probe ls "$R" --include-abstract=true --include-overview=true
ov_probe ls "$R" --include-abstract=true --include-overview=true --abs-limit 20 --overview-limit 30
ov_probe ls "$R"
```

| ls 请求 | a-empty abstract 长度 | overview 长度 | 尾部 |
|---|---:|---:|---|
| 默认 limit，显式开启两字段 | 256 | 4000 | 两者均 `...` |
| abs=20, overview=30 | 20 | 30 | 两者均 `...` |
| 默认，不开启 overview | 256 | 不提供 overview 字段 | L0 截断 |

这里的限制是**字符长度**，非 token；省略号包含在限额中，原文件 read 不被 ls 的预算截短。源码 `storage/viking_fs/_ops.py:1658-1663` 用 `summary[:limit-3] + "..."`。

CLI 一个额外坑：帮助文案写 `<true|false>`，实跑分离的 `--include-abstract true --include-overview true` 报 `Unexpected argument: true`；使用 `--include-abstract=true --include-overview=true` 成功。这次错误保留在附录命令台账，未隐藏。

长文本替换/列表窗口的 observer Calls 前后均 63，Token 总数均 424539；它证实**这一窗口**无新增 flash 统计，不代表最初 cp 或清理没有模型调用。

3b 建议：经理导航不要只靠 ls 截断预览；明确 read/abstract/overview 深化动作与资源 token 预算；若语义刷新入口未禁用，员工座位仍可能被覆盖。

## P4 · 公司 URI 重复：当前源码定位（只读）

### 结论：当前函数只追加一次公司段，指定历史 URI 没有复现

确切调用链和行号：

| 文件:行号 | 当前逻辑 |
|---|---|
| `app/memory_paths.py:5` | MEMORY_ROOT 直接取 SAM_MEMORY_ROOT_URI，去末尾 `/` |
| `app/runtime_config.py:209-213` | 同样从显式 env 注入产品根，缺失 fail-fast；不验证是否已经公司化 |
| `app/providers.py:32-37` | config.memory_root_uri 原样传入 OpenVikingMemoryProvider.memory_root |
| `app/ports.py:295,306` | 保存 memory_root，去末尾 `/`；不追加 company |
| `app/memory/company_facts/l1_brief.py:72,75-76` | self.root 取 memory.memory_root；调用 brief_json_uri/company_id/root 和 brief_md_uri |
| `app/memory/company_facts/paths.py:12-15` | `root.rstrip('/') + '/2b/' + quote(company_id, safe='')` |
| `app/memory/company_facts/paths.py:32-33` | brief_json_uri 调用 company_uri 后追加 `/L1/brief.json` |
| `app/memory/company_facts/l0_guide.py:25,28-30` | 同样取 provider 根，经 helper 构造 L1/L0 URI；不另追加公司 |
| `app/memory_map.py:80-81` | 使用同一 helpers 生成导航 URI |
| `webapp/app.py:334-342` | 传公共 memory provider 和宿主 job.company_id 调用 L2/L1/L0 |

用 AST 只执行当前 paths.py 的纯函数，剔除 imports、明确注入 MEMORY_ROOT/quote，避免业务启动、DB、模型和 pycache。实际执行逻辑：

```python
import ast
from pathlib import Path
from urllib.parse import quote
tree = ast.parse(Path('app/memory/company_facts/paths.py').read_text())
tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
ns = {'MEMORY_ROOT': 'viking://resources/sam', 'quote': quote}
exec(compile(tree, 'app/memory/company_facts/paths.py', 'exec'), ns)
for root in ['viking://resources/sam', 'viking://resources/sam/2b/acc-seat-20261011', 'viking://resources/sam/2b']:
    print(root, ns['brief_json_uri']('acc-seat-20261011', root=root))
```

结果：

```text
root=viking://resources/sam
 -> viking://resources/sam/2b/acc-seat-20261011/L1/brief.json
root=viking://resources/sam/2b/acc-seat-20261011
 -> viking://resources/sam/2b/acc-seat-20261011/2b/acc-seat-20261011/L1/brief.json
root=viking://resources/sam/2b
 -> viking://resources/sam/2b/2b/acc-seat-20261011/L1/brief.json
```

**后两项证实 root 类型/层级不校验会重复，但不是交接单的 `/2b/<company>/<company>/L1/brief.json` 相邻形态。** 不能据此宣称找到了那个历史 bug 的确切第二次拼接行。当前 git HEAD 的 paths.py 是占位文件，当前实现位于已有未提交变更里；不以 HEAD 占位替代工作树事实。已检查相关调用方，没有发现 helpers 结果后再追加一次公司段的逻辑。

3b 输入：将 product_root / facts_root / company_root 契约明确分开；统一 URI 构造，不靠字符串启发式吞掉重复段。补正常根、误传公司根、encoded company_id、已有导航 URI 的回归测试。饭团应提供当时原始错误 URI、SAM_MEMORY_ROOT_URI/provider.memory_root 值和版本，或经理工具请求轨迹；如果来自模型手拼，不应“修”一个当前正确 helper。**本轮不改任何上述文件。**

## 清理核验与非临时父刷新风险

按要求执行一次：

```bash
ov_probe rm "$R" --recursive
ov_probe ls "$R" --all
ov_probe ls viking://resources/sam --simple --node-limit 1000
ov_probe find 'probe seat-probe-20261011' --uri "$R" --limit 100
ov_probe find '极光蓝莓回款' --uri viking://resources/sam --tags probe_run=seat-probe-20261011
ov_probe find '琥珀水獭员工座位正文' --uri viking://resources/sam --tags probe_run=seat-probe-overview
```

- rm 成功，estimated_deleted_count=9；**同时回执 `semantic_root_uri=viking://resources/sam, semantic_status=queued`**。`FSService.rm:458-523` 自动对父目录作 freshness/语义刷新，无 vectors_only/skip-refresh 参数。未带 --wait，未额外触发强制刷新；没有为了恢复父内容再擅自写非临时路径。
- ls `_probe` = NOT_FOUND；parent simple 列表仅 acceptance / memory_maps，无 `_probe`。
- scoped `find probe` = resources/memories/skills 全空，total=0。
- 两组 tag 检索 = total=0。
- 另做全 sam 不带 tag 的 semantic `find probe seat-probe-20261011 --limit 100 --read-content`：total=15，都是已有 unrelated 语义相似项；**probe URI prefix 命中=0、精确 canary 正文命中=0**。语义检索不是 grep，不能要求所有真实相似项也消失；未删除这些旧资源。
- 随后第二次 ls=NOT_FOUND、第二次 scoped 特征短语 find=0。`/health` 仍健康。清理后 observer 曾见父 sam 语义任务 in_progress=1；这不是 `_probe` 残留，不假称全局队列当时已空。收尾再次检查：队列 pending=0、in_progress=0，随后 ls 仍为 NOT_FOUND、scoped 特征词 find 仍为 total=0；probe 未被异步任务重建。收尾 flash Calls=65（先前安全替换窗口为63），不能把清理也宣传成零模型调用。原始快照见证据附录。

## 检查与交付边界

- 新增文件仅本 findings。实验脚本、输入、命令原始日志均放 `/tmp/sam-seat-probe/`，不加入仓库。
- 无业务/OV 源码或配置编辑，无 commit/push。起始已有变更保留；使用 git diff --binary / status 前后核对。
- pytest：未运行；compileall：未运行；git diff --check：执行，见收尾核验。

## 3b 推荐落地方案

**先不要实施/放行 3b。** P2 可以确定使用 `write --processing-mode vectors_only` 的普通 F*.md、精确 URI read 或公开 context 面 `detail.resources=full`；已有 L0/L1 座位可以正文 replace 并重新 embed。P1 首选后续验证正规 OVPack 初始化入口（先解决 api_key 鉴权、格式稳定性和整根 overwrite 风险），或在实际无 VLM 配置下验证 B 的 `reindex semantic_and_vectors --recursive=false` 占位→正文替换路线；两者本轮都未闭环。**必须把 rm/cp/mv/reindex 的语义副作用纳入“彻底不依赖 flash”设计**，仅 vectors_only write 不足够。P4 按上表核查实际 root 注入和旧请求证据，不能先凭手记改正确 helper。补齐无-flash的公司 L1→模块 L1→事实卡全链及双重清理后，再选择明确、受支持的 backend 接口落地。

## 附录 A · 全部探针命令台账（实际调用）

| label | 实际命令 / HTTP | 耗时 s | exit / HTTP status |
|---|---|---:|---|
| baseline-probe | `ov -o json ls viking://resources/sam/_probe --all` | 0.037 | 1 |
| mkdir-probe | `ov -o json mkdir viking://resources/sam/_probe --description 'probe 20261011 员工座位机制临时实验，验收后清理。'` | 0.076 | 0 |
| mkdir-A | `ov -o json mkdir viking://resources/sam/_probe/a-empty --description 'probe 员工导览：紫铜蜂鸟座位实验。'` | 0.045 | 0 |
| A-create-rejected | `ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode create --processing-mode vectors_only --content '# 员工概览\n紫铜蜂鸟销售预测。'` | 0.042 | 1 |
| A-replace-rejected | `ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode replace --processing-mode vectors_only --content '# 员工概览\n紫铜蜂鸟销售预测。'` | 0.038 | 1 |
| A-copy-rejected | `ov -o json cp viking://resources/sam/_probe/a-empty/.abstract.md viking://resources/sam/_probe/a-empty/.overview.md` | 0.09 | 0 |
| export-template | `ov -o json export viking://resources/sam/_probe/a-empty /tmp/sam-seat-probe/template.ovpack` | 0.219 | 1 |
| A-after-copy-raw | `ov -o json read viking://resources/sam/_probe/a-empty/.overview.md` | 0.038 | 0 |
| A-after-copy-visible | `ov -o json overview viking://resources/sam/_probe/a-empty` | 0.04 | 0 |
| mkdir-company | `ov -o json mkdir viking://resources/sam/_probe/acc-seat-20261011 --description 'probe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。'` | 0.046 | 0 |
| mkdir-module | `ov -o json mkdir 'viking://resources/sam/_probe/acc-seat-20261011/06-销售' --description 'probe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。'` | 0.045 | 0 |
| write-F1 | `ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md' --from-file /tmp/sam-seat-probe/F1.md --mode create --processing-mode vectors_only` | 0.048 | 0 |
| write-F2 | `ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F2.md' --from-file /tmp/sam-seat-probe/F2.md --mode create --processing-mode vectors_only` | 0.049 | 0 |
| write-F3 | `ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F3.md' --from-file /tmp/sam-seat-probe/F3.md --mode create --processing-mode vectors_only` | 0.049 | 0 |
| P2-find | `ov -o json find '极光蓝莓回款' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 2 --read-content --limit 10` | 0.086 | 1 |
| P2-file-read | `ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'` | 0.04 | 0 |
| P2-list-all | `ov -o json ls 'viking://resources/sam/_probe/acc-seat-20261011/06-销售' --all --include-abstract true --include-overview true` | 0.035 | 2 |
| P2-single-abstract | `ov -o json abstract 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'` | 0.039 | 0 |
| P2-single-overview | `ov -o json overview 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'` | 0.043 | 0 |
| P2-company-read | `ov -o json abstract viking://resources/sam/_probe/acc-seat-20261011` | 0.041 | 0 |
| P2-find-retry | `ov -o json find '极光蓝莓回款' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 2 --read-content` | 0.085 | 0 |
| P2-list-all-corrected | `ov -o json ls 'viking://resources/sam/_probe/acc-seat-20261011/06-销售' --all --include-abstract=true --include-overview=true` | 0.037 | 0 |
| P1-overview-read-missing | `ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md'` | 0.035 | 1 |
| runtime-models | `HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json` | 0.091 | 200 |
| runtime-queue | `HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json` | 0.016 | 200 |
| runtime-health | `HTTP GET http://127.0.0.1:1933/health` | 0.007 | 200 |
| A-add-resource-seat | `ov -o json add-resource /tmp/sam-seat-probe/seat-overview.md --to 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md' --processing-mode vectors_only` | 0.149 | 0 |
| A-add-resource-seat-read | `ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md'` | 0.037 | 1 |
| A-add-resource-seat-overview | `ov -o json overview 'viking://resources/sam/_probe/acc-seat-20261011/06-销售'` | 0.042 | 1 |
| P2-tag-F1 | `ov -o json set-tags 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md' --tags probe_run=seat-probe-20261011` | 0.062 | 0 |
| P2-context-abstract | `HTTP POST http://127.0.0.1:1933/api/v1/search/search` | 0.069 | 200 |
| P2-context-overview | `HTTP POST http://127.0.0.1:1933/api/v1/search/search` | 0.054 | 200 |
| P2-context-full | `HTTP POST http://127.0.0.1:1933/api/v1/search/search` | 0.051 | 200 |
| P2-context-full-budget64 | `HTTP POST http://127.0.0.1:1933/api/v1/search/search` | 0.052 | 200 |
| P3-abstract-find | `ov -o json find '紫铜蜂鸟销售员工导览' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 0 --read-content` | 0.083 | 0 |
| P3-company-list | `ov -o json ls viking://resources/sam/_probe/acc-seat-20261011 --include-abstract=true --include-overview=true --abs-limit 20 --overview-limit 30` | 0.056 | 0 |
| queue-pre-seat-write | `HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json` | 0.02 | 200 |
| A-copy-overview-replace | `ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode replace --processing-mode vectors_only --content '# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011'` | 0.057 | 0 |
| A-copy-overview-replace-raw | `ov -o json read viking://resources/sam/_probe/a-empty/.overview.md` | 0.04 | 0 |
| A-copy-overview-replace-visible | `ov -o json overview viking://resources/sam/_probe/a-empty` | 0.047 | 0 |
| A-copy-overview-find | `ov -o json find '琥珀水獭员工座位正文' --uri viking://resources/sam/_probe/a-empty --level 1 --read-content` | 0.085 | 0 |
| models-after-copy | `HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json` | 0.031 | 200 |
| P3-seat-tag | `ov -o json set-tags viking://resources/sam/_probe/a-empty --tags probe_run=seat-probe-overview` | 0.047 | 0 |
| P3-seat-context-overview | `HTTP POST http://127.0.0.1:1933/api/v1/search/search` | 0.054 | 200 |
| P4-pure-path-reproduction | `python3: AST execute current paths.py functions (imports omitted; MEMORY_ROOT=viking://resources/sam)` | 0 | 0 |
| cleanup-preflight-parent-freshness | `ov -o json read viking://resources/sam/.abstract.md` | 0.052 | 0 |
| no-flash-window-start | `HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json` | 0.039 | 200 |
| long-l0-write | `ov -o json write viking://resources/sam/_probe/a-empty/.abstract.md --from-file /tmp/sam-seat-probe/long-l0.md --processing-mode vectors_only` | 0.059 | 0 |
| long-l1-write | `ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --from-file /tmp/sam-seat-probe/long-l1.md --processing-mode vectors_only` | 0.069 | 0 |
| P3-list-default | `ov -o json ls viking://resources/sam/_probe --include-abstract=true --include-overview=true` | 0.051 | 0 |
| P3-list-short-limits | `ov -o json ls viking://resources/sam/_probe --include-abstract=true --include-overview=true --abs-limit 20 --overview-limit 30` | 0.039 | 0 |
| P3-list-default-no-overview | `ov -o json ls viking://resources/sam/_probe` | 0.048 | 0 |
| P3-list-long-raw | `ov -o json read viking://resources/sam/_probe/a-empty/.overview.md` | 0.058 | 0 |
| no-flash-window-end | `HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json` | 0.012 | 200 |
| cleanup-rm-probe | `ov -o json rm viking://resources/sam/_probe --recursive` | 0.071 | 0 |
| cleanup-ls-probe | `ov -o json ls viking://resources/sam/_probe --all` | 0.037 | 1 |
| cleanup-ls-parent | `ov -o json ls viking://resources/sam --simple --node-limit 1000` | 0.038 | 0 |
| cleanup-find-probe | `ov -o json find 'probe seat-probe-20261011' --uri viking://resources/sam/_probe --limit 100` | 0.093 | 0 |
| cleanup-find-probe-global | `ov -o json find 'probe seat-probe-20261011' --uri viking://resources/sam --limit 100 --read-content` | 0.107 | 0 |
| cleanup-find-fact-tag | `ov -o json find '极光蓝莓回款' --uri viking://resources/sam --tags probe_run=seat-probe-20261011` | 0.077 | 0 |
| cleanup-health | `HTTP GET http://127.0.0.1:1933/health` | 0.008 | 200 |
| cleanup-queue | `HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json` | 0.011 | 200 |
| cleanup-find-overview-tag | `ov -o json find '琥珀水獭员工座位正文' --uri viking://resources/sam --tags probe_run=seat-probe-overview` | 0.09 | 0 |
| cleanup-final-ls | `ov -o json ls viking://resources/sam/_probe --all` | 0.038 | 1 |
| cleanup-final-find | `ov -o json find '极光蓝莓回款 琥珀水獭 probe' --uri viking://resources/sam/_probe --limit 100` | 0.08 | 0 |
| cleanup-final-queue | `HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json` | 0.014 | 200 |
| cleanup-settled-queue | `HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json` | 0.014 | 200 |
| cleanup-settled-models | `HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json` | 0.012 | 200 |
| cleanup-settled-ls | `ov -o json ls viking://resources/sam/_probe --all` | 0.038 | 1 |
| cleanup-settled-find | `ov -o json find 'probe 极光蓝莓回款 琥珀水獭' --uri viking://resources/sam/_probe --limit 100` | 0.077 | 0 |

HTTP body 见下方原文证据；日志标签 `A-copy-rejected` 是实验脚本的预测名称，**真实返回成功**，不能按标签认定失败。
另有一次探针脚本自身错误：第一次 HTTP find 调用后日志器取 `urllib.request.Request.method` 报 `AttributeError: 'Request' object has no attribute 'method'`；改为 `get_method()`。HTTP 调用已发出，但该返回未被记录，不拿它做验收证据。首次 export 后尝试读不存在的模板文件也报 FileNotFoundError，未重复 export。这些只修改了 /tmp 探针脚本。

## 附录 B · 关键原始返回

以下复制 subprocess stdout/stderr 或 HTTP response 原文，不重写结果字段；长正文/无关现有公司内容不在这里整段重复。

### runtime-health · 0.007s / 200

```text
HTTP GET http://127.0.0.1:1933/health
{"status":"ok","healthy":true,"version":"0.4.23","auth_mode":"dev"}
```

### mkdir-probe · 0.076s / 0

```text
ov -o json mkdir viking://resources/sam/_probe --description 'probe 20261011 员工座位机制临时实验，验收后清理。'
{"ok":true,"result":{"uri":"viking://resources/sam/_probe"}}

```

### mkdir-A · 0.045s / 0

```text
ov -o json mkdir viking://resources/sam/_probe/a-empty --description 'probe 员工导览：紫铜蜂鸟座位实验。'
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/a-empty"}}

```

### A-create-rejected · 0.042s / 1

```text
ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode create --processing-mode vectors_only --content '# 员工概览
紫铜蜂鸟销售预测。'
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"cannot create generated abstract overview directly: viking://resources/sam/_probe/a-empty/.overview.md","details":{}}}

```

### A-replace-rejected · 0.038s / 1

```text
ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode replace --processing-mode vectors_only --content '# 员工概览
紫铜蜂鸟销售预测。'
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"cannot create generated abstract overview directly: viking://resources/sam/_probe/a-empty/.overview.md","details":{}}}

```

### A-copy-rejected · 0.09s / 0

```text
ov -o json cp viking://resources/sam/_probe/a-empty/.abstract.md viking://resources/sam/_probe/a-empty/.overview.md
{"ok":true,"result":{"operation_id":"136d0fa8118243e7acabb23babda67bd","operation":"copy","from":"viking://resources/sam/_probe/a-empty/.abstract.md","to":"viking://resources/sam/_probe/a-empty/.overview.md","recursive":false,"phase":"completed","files_created":1,"vectors":{"scanned":0,"written":0,"deleted":0,"restored":0,"batches":1},"semantic_root_uri":"viking://resources/sam/_probe/a-empty","semantic_status":"queued"}}

```

### export-template · 0.219s / 1

```text
ov -o json export viking://resources/sam/_probe/a-empty /tmp/sam-seat-probe/template.ovpack
{"ok":false,"error":{"code":"PERMISSION_DENIED","message":"Admin API requires api_key mode with root_api_key configured. Development mode does not support account or user management. You should set server.auth_mode = \"api_key\" in ov.conf","details":{}}}

```

### A-after-copy-raw · 0.038s / 0

```text
ov -o json read viking://resources/sam/_probe/a-empty/.overview.md
{"ok":true,"result":"---\ndirectory: viking://resources/sam/_probe/a-empty/\ngenerated_by:\n  component: FSService\n  trigger: mkdir\nfreshness:\n  total_entries: 0\n  sampled_entries: 0\n  unsampled_entries: 0\n  pending_child_changes: 1\n---\n\nprobe 员工导览：紫铜蜂鸟座位实验。\n"}

```

### A-after-copy-visible · 0.04s / 0

```text
ov -o json overview viking://resources/sam/_probe/a-empty
{"ok":true,"result":"probe 员工导览：紫铜蜂鸟座位实验。"}

```

### mkdir-company · 0.046s / 0

```text
ov -o json mkdir viking://resources/sam/_probe/acc-seat-20261011 --description 'probe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。'
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/acc-seat-20261011"}}

```

### mkdir-module · 0.045s / 0

```text
ov -o json mkdir 'viking://resources/sam/_probe/acc-seat-20261011/06-销售' --description 'probe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。'
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售"}}

```

### write-F1 · 0.048s / 0

```text
ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md' --from-file /tmp/sam-seat-probe/F1.md --mode create --processing-mode vectors_only
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","root_uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售","context_type":"resource","mode":"create","written_bytes":238,"content_updated":true,"semantic_status":"skipped","vector_status":"queued","queue_status":null}}

```

### write-F2 · 0.049s / 0

```text
ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F2.md' --from-file /tmp/sam-seat-probe/F2.md --mode create --processing-mode vectors_only
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F2.md","root_uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售","context_type":"resource","mode":"create","written_bytes":238,"content_updated":true,"semantic_status":"skipped","vector_status":"queued","queue_status":null}}

```

### write-F3 · 0.049s / 0

```text
ov -o json write 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F3.md' --from-file /tmp/sam-seat-probe/F3.md --mode create --processing-mode vectors_only
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F3.md","root_uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售","context_type":"resource","mode":"create","written_bytes":238,"content_updated":true,"semantic_status":"skipped","vector_status":"queued","queue_status":null}}

```

### P2-find · 0.086s / 1

```text
ov -o json find '极光蓝莓回款' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 2 --read-content --limit 10
cmd: ov find --uri=viking://resources/sam/_probe/acc-seat-20261011 -n 10 --level 2 --read-content "极光蓝莓回款"
{"ok":false,"error":{"code":"INTERNAL","message":"Internal server error","details":null}}

```

### P2-find-retry · 0.085s / 0

```text
ov -o json find '极光蓝莓回款' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 2 --read-content
cmd: ov find --uri=viking://resources/sam/_probe/acc-seat-20261011 -n 10 --level 2 --read-content "极光蓝莓回款"
{"ok":true,"result":{"memories":[],"resources":[{"context_type":"resource","uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","level":2,"score":0.8300111293792725,"abstract":"","tags":[],"content":"---\nattribution_id: null\nfact_id: 1\n---\n# F1 销售事实卡\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。\n"},{"context_type":"resource","uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F2.md","level":2,"score":0.6656842231750488,"abstract":"","tags":[],"content":"---\nattribution_id: null\nfact_id: 2\n---\n# F2 销售事实卡\nprobe seat-probe-20261011 月岩蜜柚合同：acc-seat-20261011 公司 2026 年销售金额 45678 元。\n出处：伪造验收原件 sha256，页码 2，locator probe-row-2。\n"},{"context_type":"resource","uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F3.md","level":2,"score":0.6599353551864624,"abstract":"","tags":[],"content":"---\nattribution_id: null\nfact_id: 3\n---\n# F3 销售事实卡\nprobe seat-probe-20261011 青瓷海豚续费：acc-seat-20261011 公司 2026 年销售金额 23456 元。\n出处：伪造验收原件 sha256，页码 3，locator probe-row-3。\n"}],"skills":[],"total":3}}

```

### P2-file-read · 0.04s / 0

```text
ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'
{"ok":true,"result":"---\nattribution_id: null\nfact_id: 1\n---\n# F1 销售事实卡\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。\n"}

```

### P2-list-all-corrected · 0.037s / 0

```text
ov -o json ls 'viking://resources/sam/_probe/acc-seat-20261011/06-销售' --all --include-abstract=true --include-overview=true
cmd: ov ls viking://resources/sam/_probe/acc-seat-20261011/06-销售 -l 256 -n 256 --include-abstract=true --include-overview=true --overview-limit 4000 -a
{"ok":true,"result":[{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/.abstract.md","size":332,"isDir":false,"modTime":"2026-10-10T18:29:06.000Z","abstract":"","overview":""},{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","size":238,"isDir":false,"modTime":"2026-10-10T18:29:06.000Z","abstract":"","overview":""},{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F2.md","size":238,"isDir":false,"modTime":"2026-10-10T18:29:06.000Z","abstract":"","overview":""},{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F3.md","size":238,"isDir":false,"modTime":"2026-10-10T18:29:06.000Z","abstract":"","overview":""}],"has_more":false}

```

### P2-single-abstract · 0.039s / 0

```text
ov -o json abstract 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'
{"ok":true,"result":"probe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。"}

```

### P2-single-overview · 0.043s / 0

```text
ov -o json overview 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md'
{"ok":true,"result":"# viking://resources/sam/_probe/acc-seat-20261011/06-销售\n\n[Directory overview is not ready]"}

```

### P1-overview-read-missing · 0.035s / 1

```text
ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md'
{"ok":false,"error":{"code":"NOT_FOUND","message":"File not found: viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md","details":{"type":"file","resource":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md"}}}

```

### A-add-resource-seat · 0.149s / 0

```text
ov -o json add-resource /tmp/sam-seat-probe/seat-overview.md --to 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md' --processing-mode vectors_only
{"ok":true,"result":{"status":"success","task_id":"388cb8d0-20d0-44a8-aba6-af15c0e79ba7","source_path":"seat-overview.md","root_uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md"}}

```

### A-add-resource-seat-read · 0.037s / 1

```text
ov -o json read 'viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md'
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"Directory URI is not readable as a file: viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md. List it first, then read a file URI.","details":{"resource":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md","expected":"file","actual":"directory"}}}

```

### A-add-resource-seat-overview · 0.042s / 1

```text
ov -o json overview 'viking://resources/sam/_probe/acc-seat-20261011/06-销售'
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"plugin error: is a directory: /default/resources/sam/_probe/acc-seat-20261011/06-销售/.overview.md","details":{"resource":"viking://resources/sam/_probe/acc-seat-20261011/06-销售"}}}

```

### A-copy-overview-replace · 0.057s / 0

```text
ov -o json write viking://resources/sam/_probe/a-empty/.overview.md --mode replace --processing-mode vectors_only --content '# 员工概览
probe 琥珀水獭员工座位正文，禁止由 flash 编写。
署名 seat-probe-20261011'
{"ok":true,"result":{"uri":"viking://resources/sam/_probe/a-empty/.overview.md","root_uri":"viking://resources/sam/_probe/a-empty","context_type":"resource","mode":"replace","written_bytes":106,"content_updated":true,"semantic_status":"skipped","vector_status":"queued","queue_status":null}}

```

### A-copy-overview-replace-raw · 0.04s / 0

```text
ov -o json read viking://resources/sam/_probe/a-empty/.overview.md
{"ok":true,"result":"---\ndirectory: viking://resources/sam/_probe/a-empty/\ngenerated_by:\n  component: SemanticProcessor\n  trigger: content_copy\nfreshness:\n  total_entries: 0\n  sampled_entries: 0\n  unsampled_entries: 0\n  pending_child_changes: 0\n  missing_summary_entries: 0\n---\n\n# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011\n"}

```

### A-copy-overview-replace-visible · 0.047s / 0

```text
ov -o json overview viking://resources/sam/_probe/a-empty
{"ok":true,"result":"# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011"}

```

### A-copy-overview-find · 0.085s / 0

```text
ov -o json find '琥珀水獭员工座位正文' --uri viking://resources/sam/_probe/a-empty --level 1 --read-content
cmd: ov find --uri=viking://resources/sam/_probe/a-empty -n 10 --level 1 --read-content "琥珀水獭员工座位正文"
{"ok":true,"result":{"memories":[],"resources":[{"context_type":"resource","uri":"viking://resources/sam/_probe/a-empty/.overview.md","level":1,"score":0.906363844871521,"abstract":"# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011","tags":[],"content":"---\ndirectory: viking://resources/sam/_probe/a-empty/\ngenerated_by:\n  component: SemanticProcessor\n  trigger: content_copy\nfreshness:\n  total_entries: 0\n  sampled_entries: 0\n  unsampled_entries: 0\n  pending_child_changes: 0\n  missing_summary_entries: 0\n---\n\n# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011\n"}],"skills":[],"total":1}}

```

### P2-context-abstract · 0.069s / 200

```text
HTTP POST http://127.0.0.1:1933/api/v1/search/search
REQUEST BODY: {"query": "极光蓝莓回款", "mode": "context", "query_expansion": "off", "rewrite": false, "max_tokens": 2000, "quotas": {"resources": 3, "skills": 0, "events": 0, "entities": 0, "preferences": 0, "experiences": 0}, "tags": ["probe_run=seat-probe-20261011"], "telemetry": true, "detail": {"resources": "abstract"}}
{"status":"ok","result":{"entries":[{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","category":"resources","score":0.8300111293792725,"detail":"uri","text":"","origin":"self"}],"rendered":"<memory uri=\"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md\" type=\"resources\" score=\"0.83\" detail=\"uri\" />","digest":"","stats":{"searched":{"resources":1},"candidates":1,"excluded":0,"origins":{"actor_peer":0,"self":1,"other_peer":0},"peer_scope":"all","quotas":{"resources":3,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},"max_tokens":2000,"used_tokens":33,"per_entry_cap":4000,"returned":1,"dropped":0,"deduped":0,"detail":{"resources":"abstract"},"tier_counts":{"uri":1},"fill":{"floor_tokens":33,"overview_upgrades":0,"full_upgrades":0,"spare_upgrades":0},"purpose":null,"query_expansion":"off","planned_queries":["极光蓝莓回款"],"score_threshold":null,"other_peer_penalties":{"events":0.1,"entities":0.1,"preferences":0.02,"experiences":0.02,"resources":0.02,"skills":0.02,"memories":0.1},"rewrite":"off","rewrite_usage":null,"dedup":{"turns":0,"status":"off","cooled":0,"turn":null}}},"telemetry":{"id":"tm_219ee8549c5b404892f6866a605c735f","summary":{"operation":"search.context","status":"ok","duration_ms":55.229,"vector":{"searches":2,"scored":1,"passed":1,"scanned":1,"scan_reason":""},"search":{"embed_query":{"duration_ms":89.004},"vector_retrieval":{"duration_ms":3.937}}}}}
```

### P2-context-overview · 0.054s / 200

```text
HTTP POST http://127.0.0.1:1933/api/v1/search/search
REQUEST BODY: {"query": "极光蓝莓回款", "mode": "context", "query_expansion": "off", "rewrite": false, "max_tokens": 2000, "quotas": {"resources": 3, "skills": 0, "events": 0, "entities": 0, "preferences": 0, "experiences": 0}, "tags": ["probe_run=seat-probe-20261011"], "telemetry": true, "detail": {"resources": "overview"}}
{"status":"ok","result":{"entries":[{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","category":"resources","score":0.8300111293792725,"detail":"overview","text":"# F1 销售事实卡\n\n---\nattribution_id: null\nfact_id: 1\n---\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。","origin":"self"}],"rendered":"<memory uri=\"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md\" type=\"resources\" score=\"0.83\" detail=\"overview\">\n# F1 销售事实卡\n\n---\nattribution_id: null\nfact_id: 1\n---\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。\n</memory>","digest":"","stats":{"searched":{"resources":1},"candidates":1,"excluded":0,"origins":{"actor_peer":0,"self":1,"other_peer":0},"peer_scope":"all","quotas":{"resources":3,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},"max_tokens":2000,"used_tokens":122,"per_entry_cap":4000,"returned":1,"dropped":0,"deduped":0,"detail":{"resources":"overview"},"tier_counts":{"overview":1},"fill":{"floor_tokens":122,"overview_upgrades":0,"full_upgrades":0,"spare_upgrades":0},"purpose":null,"query_expansion":"off","planned_queries":["极光蓝莓回款"],"score_threshold":null,"other_peer_penalties":{"events":0.1,"entities":0.1,"preferences":0.02,"experiences":0.02,"resources":0.02,"skills":0.02,"memories":0.1},"rewrite":"off","rewrite_usage":null,"dedup":{"turns":0,"status":"off","cooled":0,"turn":null}}},"telemetry":{"id":"tm_f80608e4f86b4f278ee5d33a5ecbee03","summary":{"operation":"search.context","status":"ok","duration_ms":46.213,"vector":{"searches":2,"scored":1,"passed":1,"returned":1,"scanned":1,"scan_reason":""},"search":{"embed_query":{"duration_ms":80.511},"vector_retrieval":{"duration_ms":3.059}}}}}
```

### P2-context-full · 0.051s / 200

```text
HTTP POST http://127.0.0.1:1933/api/v1/search/search
REQUEST BODY: {"query": "极光蓝莓回款", "mode": "context", "query_expansion": "off", "rewrite": false, "max_tokens": 2000, "quotas": {"resources": 3, "skills": 0, "events": 0, "entities": 0, "preferences": 0, "experiences": 0}, "tags": ["probe_run=seat-probe-20261011"], "telemetry": true, "detail": {"resources": "full"}}
{"status":"ok","result":{"entries":[{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","category":"resources","score":0.8300111293792725,"detail":"full","text":"---\nattribution_id: null\nfact_id: 1\n---\n# F1 销售事实卡\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。","origin":"self"}],"rendered":"<memory uri=\"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md\" type=\"resources\" score=\"0.83\" detail=\"full\">\n---\nattribution_id: null\nfact_id: 1\n---\n# F1 销售事实卡\nprobe seat-probe-20261011 极光蓝莓回款：acc-seat-20261011 公司 2026 年销售金额 12345 元。\n出处：伪造验收原件 sha256，页码 1，locator probe-row-1。\n</memory>","digest":"","stats":{"searched":{"resources":1},"candidates":1,"excluded":0,"origins":{"actor_peer":0,"self":1,"other_peer":0},"peer_scope":"all","quotas":{"resources":3,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},"max_tokens":2000,"used_tokens":121,"per_entry_cap":4000,"returned":1,"dropped":0,"deduped":0,"detail":{"resources":"full"},"tier_counts":{"full":1},"fill":{"floor_tokens":121,"overview_upgrades":0,"full_upgrades":0,"spare_upgrades":0},"purpose":null,"query_expansion":"off","planned_queries":["极光蓝莓回款"],"score_threshold":null,"other_peer_penalties":{"events":0.1,"entities":0.1,"preferences":0.02,"experiences":0.02,"resources":0.02,"skills":0.02,"memories":0.1},"rewrite":"off","rewrite_usage":null,"dedup":{"turns":0,"status":"off","cooled":0,"turn":null}}},"telemetry":{"id":"tm_79172a66378546499731dc08ec923527","summary":{"operation":"search.context","status":"ok","duration_ms":44.024,"vector":{"searches":2,"scored":1,"passed":1,"scanned":1,"scan_reason":""},"search":{"embed_query":{"duration_ms":79.238},"vector_retrieval":{"duration_ms":2.964}}}}}
```

### P2-context-full-budget64 · 0.052s / 200

```text
HTTP POST http://127.0.0.1:1933/api/v1/search/search
REQUEST BODY: {"query": "极光蓝莓回款", "mode": "context", "query_expansion": "off", "rewrite": false, "max_tokens": 64, "quotas": {"resources": 3, "skills": 0, "events": 0, "entities": 0, "preferences": 0, "experiences": 0}, "tags": ["probe_run=seat-probe-20261011"], "telemetry": true, "detail": {"resources": "full"}}
{"status":"ok","result":{"entries":[{"uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md","category":"resources","score":0.8300111293792725,"detail":"uri","text":"","origin":"self"}],"rendered":"<memory uri=\"viking://resources/sam/_probe/acc-seat-20261011/06-销售/F1.md\" type=\"resources\" score=\"0.83\" detail=\"uri\" />","digest":"","stats":{"searched":{"resources":1},"candidates":1,"excluded":0,"origins":{"actor_peer":0,"self":1,"other_peer":0},"peer_scope":"all","quotas":{"resources":3,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},"max_tokens":64,"used_tokens":33,"per_entry_cap":128,"returned":1,"dropped":0,"deduped":0,"detail":{"resources":"full"},"tier_counts":{"uri":1},"fill":{"floor_tokens":33,"overview_upgrades":0,"full_upgrades":0,"spare_upgrades":0},"purpose":null,"query_expansion":"off","planned_queries":["极光蓝莓回款"],"score_threshold":null,"other_peer_penalties":{"events":0.1,"entities":0.1,"preferences":0.02,"experiences":0.02,"resources":0.02,"skills":0.02,"memories":0.1},"rewrite":"off","rewrite_usage":null,"dedup":{"turns":0,"status":"off","cooled":0,"turn":null}}},"telemetry":{"id":"tm_f84eaebd6a3c465bb4d56498784461f1","summary":{"operation":"search.context","status":"ok","duration_ms":43.933,"vector":{"searches":2,"scored":1,"passed":1,"returned":1,"scanned":1,"scan_reason":""},"search":{"embed_query":{"duration_ms":78.801},"vector_retrieval":{"duration_ms":2.701}}}}}
```

### P3-abstract-find · 0.083s / 0

```text
ov -o json find '紫铜蜂鸟销售员工导览' --uri viking://resources/sam/_probe/acc-seat-20261011 --level 0 --read-content
cmd: ov find --uri=viking://resources/sam/_probe/acc-seat-20261011 -n 10 --level 0 --read-content "紫铜蜂鸟销售员工导览"
{"ok":true,"result":{"memories":[],"resources":[{"context_type":"resource","uri":"viking://resources/sam/_probe/acc-seat-20261011/.abstract.md","level":0,"score":0.8918378353118896,"abstract":"probe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。","tags":[],"content":"---\ndirectory: viking://resources/sam/_probe/acc-seat-20261011/\ngenerated_by:\n  component: FSService\n  trigger: mkdir\nfreshness:\n  total_entries: 0\n  sampled_entries: 0\n  unsampled_entries: 0\n  pending_child_changes: 0\n---\n\nprobe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。\n"},{"context_type":"resource","uri":"viking://resources/sam/_probe/acc-seat-20261011/06-销售/.abstract.md","level":0,"score":0.8338383436203003,"abstract":"probe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。","tags":[],"content":"---\ndirectory: viking://resources/sam/_probe/acc-seat-20261011/06-销售/\ngenerated_by:\n  component: FSService\n  trigger: mkdir\nfreshness:\n  total_entries: 0\n  sampled_entries: 0\n  unsampled_entries: 0\n  pending_child_changes: 0\n---\n\nprobe 紫铜蜂鸟销售员工导览：极光蓝莓回款，事实入口 F1.md、F2.md、F3.md。\n"}],"skills":[],"total":2}}

```

### P3-seat-context-overview · 0.054s / 200

```text
HTTP POST http://127.0.0.1:1933/api/v1/search/search
REQUEST BODY: {"query": "琥珀水獭员工座位正文", "mode": "context", "query_expansion": "off", "rewrite": false, "max_tokens": 2000, "quotas": {"resources": 2, "skills": 0, "events": 0, "entities": 0, "preferences": 0, "experiences": 0}, "tags": ["probe_run=seat-probe-overview"], "detail": {"resources": "overview"}}
{"status":"ok","result":{"entries":[{"uri":"viking://resources/sam/_probe/a-empty","category":"resources","score":0.906363844871521,"detail":"overview","text":"# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011","origin":"self"}],"rendered":"<memory uri=\"viking://resources/sam/_probe/a-empty\" type=\"resources\" score=\"0.91\" detail=\"overview\">\n# 员工概览\nprobe 琥珀水獭员工座位正文，禁止由 flash 编写。\n署名 seat-probe-20261011\n</memory>","digest":"","stats":{"searched":{"resources":1},"candidates":1,"excluded":0,"origins":{"actor_peer":0,"self":1,"other_peer":0},"peer_scope":"all","quotas":{"resources":2,"skills":0,"events":0,"entities":0,"preferences":0,"experiences":0},"max_tokens":2000,"used_tokens":72,"per_entry_cap":4000,"returned":1,"dropped":0,"deduped":0,"detail":{"resources":"overview"},"tier_counts":{"overview":1},"fill":{"floor_tokens":72,"overview_upgrades":0,"full_upgrades":0,"spare_upgrades":0},"purpose":null,"query_expansion":"off","planned_queries":["琥珀水獭员工座位正文"],"score_threshold":null,"other_peer_penalties":{"events":0.1,"entities":0.1,"preferences":0.02,"experiences":0.02,"resources":0.02,"skills":0.02,"memories":0.1},"rewrite":"off","rewrite_usage":null,"dedup":{"turns":0,"status":"off","cooled":0,"turn":null}}}}
```

### P3-list-short-limits · 0.039s / 0

```text
ov -o json ls viking://resources/sam/_probe --include-abstract=true --include-overview=true --abs-limit 20 --overview-limit 30
cmd: ov ls viking://resources/sam/_probe -l 20 -n 256 --include-abstract=true --include-overview=true --overview-limit 30
{"ok":true,"result":[{"uri":"viking://resources/sam/_probe/a-empty","size":0,"isDir":true,"modTime":"2026-10-10T18:33:22.000Z","abstract":"probe 琥珀水獭员工导览：员工...","overview":"# 员工概览\nprobe 琥珀水獭员工座位正文。\n员工..."},{"uri":"viking://resources/sam/_probe/acc-seat-20261011","size":0,"isDir":true,"modTime":"2026-10-10T18:29:06.000Z","abstract":"probe 紫铜蜂鸟公司员工导览：...","overview":"# viking://resources/sam/_p..."}],"has_more":false}

```

### P3-list-default-no-overview · 0.048s / 0

```text
ov -o json ls viking://resources/sam/_probe
cmd: ov ls viking://resources/sam/_probe -l 256 -n 256
{"ok":true,"result":[{"uri":"viking://resources/sam/_probe/a-empty","size":0,"isDir":true,"modTime":"2026-10-10T18:33:22.000Z","abstract":"probe 琥珀水獭员工导览：员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实验。员工编写，只描述临时座位实..."},{"uri":"viking://resources/sam/_probe/acc-seat-20261011","size":0,"isDir":true,"modTime":"2026-10-10T18:29:06.000Z","abstract":"probe 紫铜蜂鸟公司员工导览：销售请下钻 06-销售，员工署名 seat-probe。"}],"has_more":false}

```

### no-flash-window-start · 0.039s / 200

```text
HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json
{"status":"ok","result":{"name":"models","is_healthy":true,"has_errors":false,"status":{"account_id":"default","embedding_dimension":512,"vlm":[{"Model":"qwen3.8-flash","Provider":"openai","Calls":63,"Prompt":74153,"Completion":350386,"Total":424539,"Last Updated":"2026-10-11T02:30:22.749Z"}],"embedding":[{"Model":"bge-small-zh-v1.5-f16","Provider":"local","Calls":137,"Prompt":52614,"Completion":0,"Total":52614,"Last Updated":"2026-10-11T02:32:00.490Z"}],"rerank":[]}},"error":null,"telemetry":null,"profile":null}
```

### no-flash-window-end · 0.012s / 200

```text
HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json
{"status":"ok","result":{"name":"models","is_healthy":true,"has_errors":false,"status":{"account_id":"default","embedding_dimension":512,"vlm":[{"Model":"qwen3.8-flash","Provider":"openai","Calls":63,"Prompt":74153,"Completion":350386,"Total":424539,"Last Updated":"2026-10-11T02:30:22.749Z"}],"embedding":[{"Model":"bge-small-zh-v1.5-f16","Provider":"local","Calls":138,"Prompt":52964,"Completion":0,"Total":52964,"Last Updated":"2026-10-11T02:33:22.287Z"}],"rerank":[]}},"error":null,"telemetry":null,"profile":null}
```

### P4-pure-path-reproduction · 0s / 0

```text
python3: AST execute current paths.py functions (imports omitted; MEMORY_ROOT=viking://resources/sam)
{
  "viking://resources/sam": "viking://resources/sam/2b/acc-seat-20261011/L1/brief.json",
  "viking://resources/sam/2b/acc-seat-20261011": "viking://resources/sam/2b/acc-seat-20261011/2b/acc-seat-20261011/L1/brief.json",
  "viking://resources/sam/2b": "viking://resources/sam/2b/2b/acc-seat-20261011/L1/brief.json"
}
```

### cleanup-rm-probe · 0.071s / 0

```text
ov -o json rm viking://resources/sam/_probe --recursive
{"ok":true,"result":{"uri":"viking://resources/sam/_probe","estimated_deleted_count":9,"memory_cleanup":{"status":"no_references","memory_uris":[]},"semantic_root_uri":"viking://resources/sam","semantic_status":"queued"}}

```

### cleanup-ls-probe · 0.037s / 1

```text
ov -o json ls viking://resources/sam/_probe --all
cmd: ov ls viking://resources/sam/_probe -l 256 -n 256 -a
{"ok":false,"error":{"code":"NOT_FOUND","message":"Directory not found: viking://resources/sam/_probe","details":{"type":"directory","resource":"viking://resources/sam/_probe"}}}

```

### cleanup-ls-parent · 0.038s / 0

```text
ov -o json ls viking://resources/sam --simple --node-limit 1000
cmd: ov ls viking://resources/sam -l 256 -n 1000 -s
{"ok":true,"result":["viking://resources/sam/acceptance","viking://resources/sam/memory_maps"],"has_more":false}

```

### cleanup-find-probe · 0.093s / 0

```text
ov -o json find 'probe seat-probe-20261011' --uri viking://resources/sam/_probe --limit 100
cmd: ov find --uri=viking://resources/sam/_probe -n 100 "probe seat-probe-20261011"
{"ok":true,"result":{"memories":[],"resources":[],"skills":[],"total":0}}

```

### cleanup-find-fact-tag · 0.077s / 0

```text
ov -o json find '极光蓝莓回款' --uri viking://resources/sam --tags probe_run=seat-probe-20261011
cmd: ov find --uri=viking://resources/sam -n 10 --tags probe_run=seat-probe-20261011 "极光蓝莓回款"
{"ok":true,"result":{"memories":[],"resources":[],"skills":[],"total":0}}

```

### cleanup-find-overview-tag · 0.09s / 0

```text
ov -o json find '琥珀水獭员工座位正文' --uri viking://resources/sam --tags probe_run=seat-probe-overview
cmd: ov find --uri=viking://resources/sam -n 10 --tags probe_run=seat-probe-overview "琥珀水獭员工座位正文"
{"ok":true,"result":{"memories":[],"resources":[],"skills":[],"total":0}}

```

### cleanup-final-ls · 0.038s / 1

```text
ov -o json ls viking://resources/sam/_probe --all
cmd: ov ls viking://resources/sam/_probe -l 256 -n 256 -a
{"ok":false,"error":{"code":"NOT_FOUND","message":"Directory not found: viking://resources/sam/_probe","details":{"type":"directory","resource":"viking://resources/sam/_probe"}}}

```

### cleanup-final-find · 0.08s / 0

```text
ov -o json find '极光蓝莓回款 琥珀水獭 probe' --uri viking://resources/sam/_probe --limit 100
cmd: ov find --uri=viking://resources/sam/_probe -n 100 "极光蓝莓回款 琥珀水獭 probe"
{"ok":true,"result":{"memories":[],"resources":[],"skills":[],"total":0}}

```

### cleanup-health · 0.008s / 200

```text
HTTP GET http://127.0.0.1:1933/health
{"status":"ok","healthy":true,"version":"0.4.23","auth_mode":"dev"}
```

### cleanup-settled-queue · 0.014s / 200

```text
HTTP GET http://127.0.0.1:1933/api/v1/observer/queue?format=json
{"status":"ok","result":{"name":"queue","is_healthy":true,"has_errors":false,"status":{"queues":[{"queue":"Embedding","pending":0,"in_progress":0,"processed":133,"requeued":0,"errors":0,"total":133},{"queue":"Semantic","pending":0,"in_progress":0,"processed":62,"requeued":2,"errors":0,"total":62},{"queue":"ExternalParse","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0},{"queue":"AddResource","pending":0,"in_progress":0,"processed":1,"requeued":0,"errors":0,"total":1},{"queue":"Reindex","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0},{"queue":"SessionCommit","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0},{"queue":"ExternalTask","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0},{"queue":"UserDeletion","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0},{"queue":"Semantic-Nodes","pending":0,"in_progress":0,"processed":0,"requeued":0,"errors":0,"total":0}],"summary":{"pending":0,"in_progress":0,"processed":196,"requeued":2,"errors":0,"total":196}}},"error":null,"telemetry":null,"profile":null}
```

### cleanup-settled-models · 0.012s / 200

```text
HTTP GET http://127.0.0.1:1933/api/v1/observer/models?format=json
{"status":"ok","result":{"name":"models","is_healthy":true,"has_errors":false,"status":{"account_id":"default","embedding_dimension":512,"vlm":[{"Model":"qwen3.8-flash","Provider":"openai","Calls":65,"Prompt":76747,"Completion":359365,"Total":436112,"Last Updated":"2026-10-11T02:35:57.822Z"}],"embedding":[{"Model":"bge-small-zh-v1.5-f16","Provider":"local","Calls":150,"Prompt":59169,"Completion":0,"Total":59169,"Last Updated":"2026-10-11T02:35:58.062Z"}],"rerank":[]}},"error":null,"telemetry":null,"profile":null}
```

### cleanup-settled-ls · 0.038s / 1

```text
ov -o json ls viking://resources/sam/_probe --all
cmd: ov ls viking://resources/sam/_probe -l 256 -n 256 -a
{"ok":false,"error":{"code":"NOT_FOUND","message":"Directory not found: viking://resources/sam/_probe","details":{"type":"directory","resource":"viking://resources/sam/_probe"}}}

```

### cleanup-settled-find · 0.077s / 0

```text
ov -o json find 'probe 极光蓝莓回款 琥珀水獭' --uri viking://resources/sam/_probe --limit 100
cmd: ov find --uri=viking://resources/sam/_probe -n 100 "probe 极光蓝莓回款 琥珀水獭"
{"ok":true,"result":{"memories":[],"resources":[],"skills":[],"total":0}}

```

## 附录 C · 首轮 find 的服务 traceback

服务日志：`harness-openclaw/state/ov-data/server.log`。上层 INTERNAL 的完整最终异常链如下；前序 llama.cpp 异常最终为 `RuntimeError: llama_decode returned -1`。一次重试后成功，不再硬试。

```text
Traceback (most recent call last):
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
    await app(scope, receive, sender)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/server/timing_middleware.py", line 41, in __call__
    await self.app(scope, receive, send_timed)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/server/profile_middleware.py", line 177, in __call__
    await self.app(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/observability/http_observability_middleware.py", line 805, in __call__
    await self.app(scope, receive, send_observed)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/middleware/exceptions.py", line 63, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
    raise exc
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
    await app(scope, receive, sender)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/middleware/asyncexitstack.py", line 18, in __call__
    await self.app(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/routing.py", line 676, in __call__
    await self.middleware_stack(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 2786, in app
    await route.handle(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 1817, in handle
    await self.original_router.handle(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 2869, in handle
    await included_router._handle_selected(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 1843, in _handle_selected
    await original_route.handle(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 1308, in handle
    await effective_context.app(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 165, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
    raise exc
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
    await app(scope, receive, sender)
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 151, in app
    response = await f(request)
               ^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 727, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<3 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/fastapi/routing.py", line 360, in run_endpoint_function
    return await dependant.call(**values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/server/routers/search.py", line 386, in find
    execution = await run_operation(
                ^^^^^^^^^^^^^^^^^^^^
    ...<13 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/server/telemetry.py", line 29, in run_operation
    return await run_with_telemetry(
           ^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<3 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/telemetry/execution.py", line 167, in run_with_telemetry
    result = await fn()
             ^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/service/search_service.py", line 194, in find
    result = await viking_fs.find(
             ^^^^^^^^^^^^^^^^^^^^^
    ...<10 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/storage/viking_fs/_semantic.py", line 301, in find
    result = await retriever.retrieve(
             ^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<10 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/retrieve/hierarchical_retriever.py", line 159, in retrieve
    result: EmbedResult = await embed_compat(
                          ^^^^^^^^^^^^^^^^^^^
    ...<3 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/models/embedder/base.py", line 187, in embed_compat
    return await _embed_from_request_cache(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<2 lines>...
    )
    ^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/models/embedder/base.py", line 209, in _embed_from_request_cache
    return await factory()
           ^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/config/embedding.py", line 292, in embed_compatible
    return await self.provider.embed(self.account_id, content, is_query=is_query)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/config/embedding.py", line 225, in embed
    return await self._execute(resource, prepared, is_query)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/config/embedding.py", line 245, in _execute
    result = await resource.embedder.embed_async(content, is_query=is_query)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/models/embedder/base.py", line 358, in embed_async
    return self.embed(content, is_query=is_query)
           ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/jakki/.local/share/uv/tools/openviking/lib/python3.14/site-packages/openviking/models/embedder/local_embedders.py", line 207, in embed
    raise RuntimeError(f"Local embedding failed: {exc}") from exc
RuntimeError: Local embedding failed: llama_decode returned -1
```

## 附录 D · 最终文件核验

- existing tracked diff unchanged: PASS。
- only added report: PASS。
- HEAD unchanged: PASS。
- git diff --check: PASS。
- untracked report whitespace: PASS。
- git diff --check：exit 0，stdout/stderr 均空；未跟踪报告另用 `git diff --no-index --check /dev/null <report>` 检查，exit 1 是新增内容的差异状态，stdout/stderr 均空、无空白报错。
- 开始/结束 tracked diff SHA-256 相同：`aeb17ef2a7cbb1e5673ce900191f9b67ff9b50c09887306b7eb12f12da05de21`。
- pytest / compileall：未运行，不提供虚构 passed/skipped 数字；commit：无，push：无。
