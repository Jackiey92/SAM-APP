# 事实树骨架 · 段A完工报告

日期：2026-10-14。工作分支：`feat/unified-prototype-ui`。交接口径基于 `63b8b22`；计划仅本地提交，不 push、不部署。代码与测试已完成，但本地 commit 被只读 .git 权限阻断，尚未生成代码提交。

## 文件清单与处理

新增：
- `app/module_tree.py`：原树枝定义原样搬迁，仅替换文件说明；保留 hidden、符号和返回值。
- `app/settings.py`：集中定义 `SAM_FACTS_TREE`，默认关闭。
- `app/facts_tree/__init__.py`、`template_packs.py`：冻结数据类及只读注册表；结构强校验，不做业务分类。
- `app/facts_tree/company_binding.py`：显式建档、空仓换绑、幂等锁定、scope 校验；独立服务，不接主流程。
- `app/facts_tree/tree_meta.py`：受控 JSON 投影读写，写入受开关保护。
- `tests/test_facts_tree_packs.py`、`test_facts_tree_paths.py`、`test_facts_tree_binding.py`：共 123 个新增用例。
- 本报告。

修改：
- `app/module_taxonomy.py`：纯兼容 re-export；业务消费方旧 import 未批量修改。
- `app/memory/taxonomy.py`：保持原符号集合，改从 module_tree 转发。
- `app/db/schema.py`：只追加 IF NOT EXISTS 的 `company_tree_meta` 和 `company_tree_meta_projection`。前者为结构化正本；后者存储 tree.meta 的 JSON 投影，非向量、非 KV、非 brief/guide bundle，不影响旧投影流程。
- `app/memory/paths.py`：只新增叶子、树元数据、树枝文档的纯 URI 构造函数；旧 parse 不动。
- `app/memory/provider.py`：新增受控元数据读写与默认关闭的叶子预留写口；普通 put 禁止直接写 tree.meta。旧 publish_documents 白名单、F 卡限制及原子卡投影不变。

保留数据包：`app/facts_tree/templates/generic/leaves.yaml` 已由基底提交 `d14b9ad` 纳入版本库，本次保持字节不变，当前工作树仍包含它，未重抄/改数。

## 真实验收数字

- 开工前基线：`pytest -q -p no:cacheprovider` → **990 passed, 1 skipped，148.60s**。
- 新增专项最终：**123 passed，3.41s**。
- 最终全量：`.venv/bin/python -m pytest -q -p no:cacheprovider` → **1113 passed, 1 skipped，143.28s，0 failed/error**（990 + 123）。
- `.venv/bin/python -m compileall -q app scripts`：通过。
- `git diff --check`：通过。
- 首轮专项曾有 1 个测试断言错误：旧 bundle 对树枝目录 URI 先抛 `ValueError: unknown fact-memory module`，而非测试误期望的 PermissionError。仅修正测试，没有改变旧入口行为。

## 模板与绑定

`available_packs()` → `['generic']`。

98 叶，按树枝顺序计数：info 12 / bizov 15 / hr 9 / prod 9 / purch 4 / sales 18 / mkt 19 / fin 2 / fund 5 / legal 5。编号连续、全局唯一；跳号、非法树枝、坏 shape、重复 code、新 trunk、前缀不符、错误类型/计数/版本、重复 YAML key 等坏包全部抛 ValueError。

临时库覆盖注册/重复/非法 id/非法 pack/空仓换绑/非空拒绝/锁定拒绝/幂等，以及正本与投影一致、投影失败事务回滚。默认空仓检查按公司查询 m1_documents/m1_chunks，并额外拒绝未投影的 facts 原子记录；元数据投影不参与空仓计数。注册不生成空叶。

## 默认关闭、主流程零接线的证据

- 实测 `app.settings.SAM_FACTS_TREE == False`；环境变量缺省测试确认关闭。publish_leaves、元数据写口、绑定写操作关闭时抛 `RuntimeError("facts tree disabled")`。
- `rg -n 'SAM_FACTS_TREE|publish_leaves|CompanyBindingStore|put_tree_meta|read_tree_meta' app --glob '*.py'`：仅定义/内部转发/绑定服务调用，无 ingest、parse、classify、projection、HTTP、UI 主流程调用。环境变量只在 settings 读取。
- 与基底 HEAD 的 AST 对比确认 paths 的 parse/identifier/_segment/全部旧 URI 构造，以及 provider 的 publish_documents/rebuild_projection/_address/_project/_write 完全不变。
- 13 个既有 URI 快照覆盖公司根、模块 brief/guide、F 卡、original、公司/运行 KV；新路径归属与 scope 测试通过。当前旧 parse 本来就把含点的根文件 tree.meta 判为根，实测 module_key 为空，因此没有引入不必要的 parse 特判。
- `grep -rn taxonomy app --include='*.py'`：余项仅旧路径 import、兼容说明和旧注释，无 taxonomy 真实定义；树枝定义唯一在 module_tree。
- 未安装依赖；PyYAML 实测 6.0.3，`git diff --name-only -- 'requirements*'` 为空。
- 本次代码/测试未访问真实 DB 或原件/解析件；绑定和写口测试只用临时目录/库。harness-openclaw 既有脏文件未触碰、未暂存、未提交。

段A到此结束，不开始段B/段C。

## 本地提交阻断

已执行指定 commit 命令，失败原文：

```text
fatal: Unable to create '/home/jakki/projects/startup-ai-manager/.git/index.lock': Read-only file system
```

未绕过沙箱、未重试、未 push/部署。代码与测试文件已暂存，报告本节为失败后的工作区补记；当前 HEAD 仍为交接文档提交 `63b8b222f62868bc0955160cdcdf92a38bee080b`，不是段A代码 commit。需恢复 .git 可写后完成指定提交。
