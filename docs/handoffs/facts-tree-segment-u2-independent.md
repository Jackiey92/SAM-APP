# U2 饭团独立复核

2026-10-15，HEAD仍a1dfe7b，未提交。本次数字仅用于当前U2，不能替代U3之后的最终全量。

- 正常WSL：`tests/test_document_read_parallel.py tests/test_facts_tree_mapped_md.py`，修测试同步夹具后 **53 passed，9.63s**，日志 `/tmp/sam-u2-verified.log`。
- 关闭态、真实事务/provider及模块交付相关四文件：**87 passed，3.64s**，日志 `/tmp/sam-u2-related.log`。
- 首次专项 **3 failed / 50 passed，8.53s**：Python Barrier在/dev/shm建共享内存，被既有隔离保护正确拒绝。CRIS只将夹具改为双Event会合，不改conftest/隔离机制、不将两进程改成线程。

## 两个真实模型员工，独立于离线夹具

饭团运行私有 `.runtime/acceptance/file-driven/run_parallel_real.py`，两份专门生成的小型docx，各经独立保真转换后，员工只读映射MD。调用真实qwen3.8-flash，经run_files(workers=2)，两个worker各开独立reader/provider，共享新建隔离SQLite与假名公司。不是原项目原件验收。

实际32次模型请求，两份MD都未被整份投喂。两job五相累计分别为3/3/3/3/4和3/3/3/3/4。HR叶首稿版本均0，失去竞争的一方重读版本1再交；60人与82人同一统计时点冲突进入待办，正常叶保留先写入的60。业务技术叶保留两份文件的无冲突技术描述。共3片叶、4个内容块、4条修订、1条md待办，原子卡投影0。再次运行两个已完成job，没有模型调用、内容/事实/历史/待办重复。保护真库与原件SHA/行数/mtime前后一致。

**本次综合材料处理仍有1份拒稿，不能宣称全部通过。** 公司名称重复材料的落后员工重读后，想把旧块证据换成新文件证据（note明确“更新公司名称来源证据至新文件”），被`existing content/index must be retained`拒绝，旧内容和出处保留。独立产物审计因此FAIL，未放宽守卫。并行/重读/幂等机制已得实证；该合法纠错机会在U3收口，不用删断言或伪造成功解决。

证据已归档 `.runtime/acceptance/file-driven/u2-parallel-real/`，临时验收目录及测试数据库已清理。原trace、model响应、结构结果与独立审计为准。root脚本初版exit0仅表示两个job已处理/无异常，并非无拒稿；已改为零未解决才PASS，避免误读覆盖完成状态。此修改只在私有验收脚本，不是业务代码。
