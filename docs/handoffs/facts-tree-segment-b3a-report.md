# 事实树 B3a 回执：只读显隐、快照白名单与绑定回归

起点 HEAD：`c4191d93664093c5159a1777fa9b79bae4c11b7d`；基线 1317 passed / 1 skipped。
仅本地提交，未 push、未部署；完成停手等黑盒，不进入 B3b。

## 施工口径与裁决落实

已读取 15 唯一施工口径及 09 v1.2 §9/§10/§12.11/§12.12，核对真实构造、_active、绑定检查与两端 visible_modules。
三次代码口径冲突均曾停手，并按饭团后续明确裁决恢复：

1. 允许新增真只读装配：SQLite mode=ro URI、跳过 schema 初始化；缺库/缺表不触发建库建表，查询 OperationalError 兜空。
2. 快照形状为 list[dict]；**本棒导出和消费只允许静态非 hidden 5 枝（info/bizov/hr/mkt/legal）收窄**，不开放原 built-in hidden 5 枝。此裁决覆盖原 15 中云端门开直接开放全 10 枝的口径。显示字段和排序均来自本地静态定义。
3. _is_empty 最小增加 facts_tree_leaf_index；任意公司叶索引行（含冲突/归档/零 fact 描述叶）均视为非空。不是新增 mark_locked 副作用，锁定位及其其它路径保持原行为。

## 文件清单

- `app/facts_tree/visibility.py`：纯只读 active_trunks 与 visible_modules_for_company。
- `app/facts_tree/leaf_store.py`：open_readonly / read_only 构造；只读写入口明确拒绝。
- `app/facts_tree/company_binding.py`：只读构造跳过 DDL；只读写入口拒绝；_is_empty 加叶索引表。
- `app/cloud_export.py`：仅门开时按 active code 收窄静态 visible_modules。
- `app/cloud_home.py`：不可信快照仅提供白名单成员资格，显示内容全本地。
- `tests/test_facts_tree_visibility.py`：24 项新增回归。
- `tests/fixtures/visible_modules_b2b2.json`：从 c4191d9 的原 module_tree 实测生成静态列表 JSON 字节 golden。
- 本回执。

未修改前端、默认开关、SNAPSHOT_VERSION、依赖、模板包、原 _active/SHA/frontmatter/坐标/数字护栏、技能/catalog/工具清单。harness-openclaw/ 既有运行时脏文件不 stage；未跟踪的 13/14/15 派单原文不擅自纳入提交。

## 新 API 与只读保证

`LeafPublishService.open_readonly(db_path, memory, *, company_id=None)`（亦支持 read_only=True）借助 CompanyBindingStore 的只读路径打开 `Path.resolve().as_uri() + '?mode=ro'`，不调用普通 connect，不创建父目录、不创建数据库、不执行 schema DDL。
CompanyBindingStore 的 read_only=True 要求传数据库路径，不接受可能是可写的注入连接，保证不能用一条普通连接冒充只读。默认读写连接/DDL装配保留。

只读实例的 publish_leaf/publish_conflict/register/bind_pack/mark_locked 明确抛 PermissionError；额外写入口门用于拦住内容相同或同包重绑等可能本来不执行 SQL 的 no-op。底层 SQLite 连接本身也自然拒绝 CREATE/DELETE 等写入，不只是 Python 方法门。

- `active_trunks(*, db_path, memory, company_id)`：require_enabled；只读装配；未绑定返回 []；复用 FactsTreeRollup._active 的索引、最新 SHA、frontmatter、active/有内容判定；trunk 去重，按本地 MODULES 顺序输出；finally close。SQL OperationalError 返回 []，不迁移；LeafRejected 等完整性错误照常上抛。
- `visible_modules_for_company(...)`：全 10 枝诊断列表，字段 code/name/sort_order/hidden；hidden=该枝不在 active_trunks。系统枝不进列表。此函数保留原 15 所要求的存储查询语义，**不直接用于云端导出**，后者按最新裁决执行静态 5 枝白名单交集。

## 导出/消费门分支

cloud_export：原静态 list[dict] 初始化不变；仅 `if settings.SAM_FACTS_TREE:` 读取 active_trunks，然后按静态顺序筛选。hidden 枝即使有 active 叶也不会进入云端白名单；其它 page_context 字段不变；同一个既有 memory 实例复用，零模型调用。

cloud_home：不引入主仓 DB/memory 依赖。快照缺 visible_modules 字段时原静态全集 fallback；字段存在但非 list 时空列表；每个元素只接受 dict + str code + 命中本地白名单。重复去重，最终以本地静态顺序输出本地 dict；快照 name/sort_order/hidden 和顺序完全不采用。关闭态可信快照带原静态列表时结果与现行完全一致；恶意快照也只能收窄。

## 建档绑定结果（§12.12）

- 注册写 tree.meta 投影，template_pack=generic/pack_version=1；重复注册拒绝。
- 注册不产生空叶或叶索引；无 facts/叶的空仓同包重绑成功（不造不存在的垂类模板）。
- 发布零 fact 纯描述 active 叶后，锁定位仍按原行为不额外改变，但叶索引使 nonempty=True，bind_pack 抛 PermissionError。
- 独立冲突件（零 facts）与全部 archived 叶同样阻止重绑；写过即锁，不清索引重开。
- 缺 facts_tree_leaf_index 表时仍先查 sqlite_master，空仓检查不报错。
- 门关闭 register/bind_pack 与显隐查询被 require_enabled 拒绝，无新增写入。

## 新增/修正测试明细

新增 24 项；此前失败的 2 个本棒用例已修正：artifact company_id 改为真实宿主 acc-b3a；active 叶用例不再假定 locked 位会改变，改断言实际 nonempty 拒绝，并按裁决加 _is_empty 索引护栏。既有测试不删不放宽。

| 类别 | 项数 | 证据 |
|---|---:|---|
| 空树/未绑定、全 10 枝诊断 | 1 | 全 hidden，无系统枝 |
| prod/sales 描述叶与动态归档 | 1 | 诊断 active 随发布/归档增减；不等于云端白名单扩张 |
| 空壳/归档/冲突不显示 | 1 | _active 复用过滤，系统件不进树枝 |
| 只读 SELECT、全部写入口（含 no-op）拒绝 | 1 | _leaf/绑定/revision 可读，CREATE 自然拒绝，API 抛 PermissionError，DB dump 不变 |
| 缺表/缺路径只读打开 | 1 | 空库字节不变、sqlite_master 空、缺父目录不创建 |
| SELECT-only authorizer/记忆写失败哨兵 | 1 | 显隐查询成功，除 SELECT/READ/FUNCTION 外 SQL 禁止 |
| 原 SHA 护栏 | 1 | 篡改叶文档仍 LeafRejected，不吞成空 |
| 注册投影/重复拒绝/空仓重绑/无空叶 | 1 | 既有 API 闭环 |
| 零 fact active 描述叶拒绝换绑 | 1 | 索引非空、facts=0，不新增 mark_locked 行为 |
| 关闭态显隐与绑定零写入 | 1 | DB dump 完全相等 |
| 导出白名单顺序/hidden 不扩张 | 1 | 仅 info/bizov；prod 即便 active 也被白名单排除 |
| 导出空树/未绑定/关闭态 golden | 1 | 动态空，关闭态静态 JSON 字节匹配 baseline |
| 消费脏快照 | 1 | 未知/非 str code/字符串项/hidden code 丢弃；合法 code 篡改字段仍回填本地显示，去重与本地排序 |
| 消费非 list/空列表 | 5 | 无扩张、返回 [] |
| 消费缺字段/关闭态 golden | 1 | fallback 静态全集、现行 JSON 字节一致 |
| 独立零 fact 冲突件拒绝换绑 | 1 | 只有冲突索引也非空 |
| 仅 archived 叶仍拒绝换绑 | 1 | 写过即锁 |
| 空仓检查缺索引表 | 1 | 防护查询保持安全 |
| 只读不接受注入可写连接 | 1 | 防伪装只读 |
| 绑定只读路径自然 SQL 写拒绝 | 1 | DELETE 抛 SQLite readonly 错误 |
| 合计 | 24 | 全绿 |

专项实测：

```text
.venv/bin/python -m pytest -q tests/test_facts_tree_visibility.py
24 passed in 2.22s

.venv/bin/python -m pytest -q tests/test_facts_tree_visibility.py tests/test_facts_tree_binding.py tests/test_facts_tree_leaves.py tests/test_cloud_home.py
177 passed in 13.89s
```

初轮曾 141 passed / 2 failed，已如上修正；不是隐藏失败或放宽护栏。加入后续 5 项独立护栏测试后重新专项全绿。

## 全量与静态检查

- 全量 `.venv/bin/python -m pytest -q`：`1341 passed, 1 skipped in 151.09s (0:02:31)`，0 failed；1317 基线 + 新增 24，原测试不减。
- `.venv/bin/python -m compileall -q app scripts`：退出 0。
- `git diff --check` 与 `git diff --cached --check`：均退出 0。
- module_tree、rollup、LeafBuildService/leaf_contract、settings、cloud_snapshot、技能与 tool-registry：零 diff。新 visibility 后端 grep 无纳米/涂膜/前五大；零新依赖。
- 关闭态两端 visible_modules JSON 与 c4191d9 原静态 golden 逐字节一致；其字段仍 code/name/sort_order/hidden，静态 5 枝集合不变。

## 未做项

未改前端或开放云端 built-in hidden 枝（按安全白名单裁决）；未做 B3b 记忆层切换、B3c 真机、真模型端到端、建档新 API/新模板、清空重开、部署/push。所有数据仅临时库/本地测试文件，未触碰真实 app.db。本回执不替代饭团黑盒/真机放行，完成后停手等验收。
