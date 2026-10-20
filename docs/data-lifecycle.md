# 数据生命周期与施工隔离（G1）

## 配置与启动

迁移是管理员的显式操作，不是 import、服务启动或 provider 构造的副作用。
`PRAGMA user_version` 是 SAM 的迁移版本；缺库/版本不符时启动拒绝，并给出迁移命令。
不要对已有真实库直接照抄下面的演示迁移；先做只读基线、确认目标及停写状态。

```bash
# 演示：必须自行选择一个新的隔离目录，不能使用真实 data/ 或 /tmp/sam-m5。
export SAM_DATA_ROOT="$(mktemp -d -t sam-demo-XXXXXX)"
export SAM_MAIN_DB="$SAM_DATA_ROOT/app.db"
export SAM_2A_ROOT="$SAM_DATA_ROOT/2a"  # 原件归档/底稿根
.venv/bin/python -m app.db.migrate --db "$SAM_MAIN_DB"
# 同一目标再次执行是无写入的版本校验，不重跑种子/建表。
.venv/bin/python -m app.db.migrate --db "$SAM_MAIN_DB"
scripts/run.sh  # 另需现有的 Node/API key 配置
```

旧方式：`init_db()`/import 自动迁移，没配置也会落 `data/app.db`。
新方式：`init_db(path)` 仅供显式初始化；命令必须指定 `--db`。
`connect()` 无路径时要求 `SAM_DATA_ROOT`。运行配置可使用现有显式 profile 的
`paths.data_root`/`paths.main_db`/`paths.two_a_root`；代码不兜底真实目录。
标准 shell 启动入口要求显式 `SAM_DATA_ROOT`。覆盖数据根时其子路径随根派生，
不再继承开发 profile 中指向 checkout 的默认子路径。

云只读窗口使用独立库，启动也不建表：

```bash
.venv/bin/python -m app.db.migrate --db "$SAM_DATA_ROOT/cloud-snapshot.db" --snapshot
scripts/cloud_run.sh
```

云模式不再发明 `/tmp/sam-cloud-home`：需 `SAM_DATA_ROOT`，或模块配置中的
显式 `paths.cloud_data_root`。不会借用 local profile 的真实库根。
当前 `data/app.db` 保留事故后的五列及全部数据，**本单没有给它写版本号**；
新代码对它拒绝自动迁移，由饭团另行决定何时显式升级。

## 开工只读基线

从任意 app import 或测试前开始：

```bash
mkdir -p .runtime
.venv/bin/python scripts/db_baseline.py data/app.db /tmp/sam-m5/main.db \
  /tmp/sam-m5/material/fmt_C_info.docx > .runtime/before.json
# 完工后相同命令输出 after.json，再 cmp before.json after.json。
# 指定底稿输入根可递归记录每个原件（不移动、不删除）：
.venv/bin/python scripts/db_baseline.py --evidence-root /tmp/sam-m5/material
```

脚本输出 SHA256、mtime_ns、SQLite 全表行数；只读 immutable 连接避免创建
journal/shm，不导入 app。遇非空 WAL 拒绝生成可能误导的行数，先由宿主停写，
**脚本不会自行 checkpoint/停服务**。`.runtime/` 忽略入库，报告保留摘要。
`SAM_2A_ROOT` 是运行期原件归档根；外部源材料根由快照的 `--evidence-root`
显式提供。不配置 `SAM_2A_ROOT` 的底稿存储调用拒绝运行。
`/tmp/sam-m5/material` 易失且无备份保障，本期仅记录风险/哈希，不物理搬迁。

## 测试隔离

裸跑 `.venv/bin/python -m pytest -q` 即在收集前创建专属临时根，显式迁移临时库。
所有数据类 SAM 路径及任意 `SAM_*DB` 都重定向并启动自检；TMPDIR、pytest
basetemp、缓存亦在此根，退出清理。源码、技能、可执行文件仍只读使用。

- 父进程 audit 拒绝专属临时根之外写操作，包括目录 fd/文件 fd；保护真实
  `data/`、`/tmp/sam-m5`、重定向前的 DB 路径。
- 原生 subprocess 强制 bwrap：宿主根只读，仅本轮临时根可写，真实路径再只读遮罩；
  无 bwrap 直接失败，不降级。Python 子进程还加载继承的审计钩子；`-I` 下仍有内核只读约束。
- 父进程及普通 Python 子进程拒绝对 18790 的连接。bootstrap 自选随机空闲端口；
  端口探针回归只发审计事件，绝不以“真连接失败”作为护栏证明。
- 质检仍用原有更严格 bwrap：只挂本任务证据（无宿主底稿父目录）、隔离库/系统库，
  独立网络 namespace；不改角色授权/解析/财务规则。

这是开发回归护栏，不把任意恶意 native 子进程的网络行为当作已完全沙盒化。
普通测试 subprocess 保留 localhost 给真实 HTTP 回归，只有质检任务全面禁网。
