# 段 U：饭团 WSL 独立审查与提交前实测

2026-10-15。本轮起点a1dfe7b；实际业务提交见git log。段T原件真机未通过的历史结论仍保留，不能由本轮测试数字覆盖。

## 结果与实际边界

已实现文件驱动的映射MD阅读/归叶；叶正文仅内容和索引，独立审计；员工写前比较，冲突待办双方证据完整；真实并行入口、跨进程租约代次、同叶CAS重读、有界计费/纠错/显式续跑；QC指纹绑定的上传入口、待办读视图、L1/L0机械消费。全部业务代码通过同卡CRIS交付，饭团做架构、审查与独立实测。

默认门关；旧格式不自动迁移，争议不自动裁决。新增辅助表按门开显式操作创建，无生产数据迁移。门开上传须明确五phase累计预算（SAM_MD_PHASE_LIMITS）和公司模板绑定。普通SQL写串行，模型调用不持SQL锁。

## 当前代码真实验证

- 正常WSL全量 `.venv/bin/python -m pytest -q`：**1525 passed / 1 skipped / 0 failed，178.07s**。日志 `/tmp/sam-u-final-pytest.log`。8个Python3.14 fork多线程DeprecationWarning，测试无失败。
- `.venv/bin/python -m compileall -q app webapp tests scripts` exit 0；`git diff --check` exit 0。
- 关闭态原测试/golden、app/db、provider.py、tool-registry零diff。官方skill catalog属正式产物；harness DREAMS/memory脏文件排除提交。
- WSL独立分段：U1最终39 passed；U2最终53 passed及相关87 passed；U3三文件78 passed。均仅作分段证据，终态以上述全量为准。

## 独立真实模型与真浏览器

小型材料均为本轮明确生成的合成文件，各经原件转换及独立保真脚本；员工只拿映射MD，不冒充原项目原件验收。实际调用qwen3.8-flash、真实LocalVectorMemoryProvider/本地BGE、独立临时库，凭证仅本机环境使用。

1. 单文件四窗口：20次请求，3叶、1冲突待办、0原子卡投影；无整份MD请求。员工识别同公司同统计时点60/82人冲突，旧60保留。
2. 双文件员工通过run_files(workers=2)：实际两员工同时读人数叶版本0，失去竞争者重读版本1；两段无冲突技术信息保留于同叶，人数差异进待办。初轮32请求有1拒稿；U3纠错版35请求仍有1拒稿，原稿/错误全部保留，严格审计均如实FAIL。
3. 原trace确认“旧块完整保留”和“仅当前grant”歧义让员工替换旧来源。fact-leaf0.5.1及compare_submit静态契约明确旧证据与新增证据授权边界，后端不自动修稿、不松守卫。**同一失败job**显式续跑，仅leaf累计8→11，classify/attribute/extract/plan各3不变；3次真实额外交稿后无未解决。总38个HTTP请求，无整份MD请求；3叶/4块/4证据/4修订/4事实/1待办，严格独立审计PASS。修复前后修订/事实数均4，没有用重写历史伪造过关。旧5份公司名称坏稿及1份人数坏稿审计留存；成功稿由员工自行选择no-op保留已发布公司信息。
4. Windows真浏览器按仓库唯一脚本截图成功，`/todos`及`/api/todos` HTTP200。新内容冲突与旧数字待办分类型展示，双方60/82及理由可见，完整来源/处理记录折叠；新类型没有自动裁决按钮。截图已通过coze-file-send送达范范，源自隔离验收库。

保护数据库data/app.db、/tmp/sam-m5/main.db及真实原件SHA/行数/mtime在各轮前后一致。真实原件SHA6515c41717675c7fbb323ff959493a37ea331727fc73668f5b89deb78054a965。私有trace/结构结果归档在.runtime/acceptance/file-driven，测试库按流程清理。

## 审查发现与修正

- 默认主链曾先读取整稿：独立递归请求探测发现后改为五相均在当前窗口，上游不先业务读取全文。
- 故障注入曾发现SQL回滚后活跃进度留下幽灵fact_ids：改副本检查点同事务，commit成功后才回填活跃对象；四类故障与续跑测试通过。
- 初始原子卡断言误用大小写不敏感LIKE，将/facts路径计入；核对真实投影类型后修测试，业务护栏未放宽。
- Python Barrier写/dev/shm被隔离拒绝：只改两进程会合夹具为双Event，隔离保护未修改。
- pytest模块导入掩盖正式脚本入口settings自导入失败：调整路径自举顺序；正式文件入口门开/关smoke纳入全量。
- 长待办正文和审计字段可读性：新类型独立样式换行、保留证据并折叠处理记录，旧数字待办不改。

## 尚待完成的原件验收

本地业务提交后运行私有原件完整验收：真实原件映射为13937字符/401行/114窗口，无未映射非空行或超界窗口。五phase预算将明确给足而不沿用旧classify=2/plan=2；检查完整覆盖、叶数/未解决/待办分开计数、纳米技术段落及77坐标回查、数字/索引、检索和显式重建零原子卡、L1/L0及保护基线。未执行前不填通过结果；失败看原始trace，不放宽守卫。不push、不部署。

## 原件首次失败与空归属修复（追加）

业务提交38e3751之后，原件首次28.52s、16次HTTP，窗口3（第7行docx:body/4/paragraph）在missing window attribution停下，partial、0叶/0事实/0修订/0原子投影，保护基线一致。该段只有组织机构/部门职责的填表提示；员工classify正确判断无具体事实，attribute交ranges=[]，原接口却强制非空。保留真实FAIL，不重造验收案例。

CRIS修复只增加明确空归属回执及一次计费格式纠正：ranges=[]须outcome=no_attribution与非空员工理由，仍继续extract及独立归叶，非空事实不能绕过原实体范围/quote/数字守卫。无后端标题或关键词识别。file-processor1.2.1、fact-leaf0.5.2及官方catalog同步，新增9例见empty-attribution报告。

饭团正常WSL修复后全量：**1534 passed / 1 skipped / 0 failed，167.71s**（8个既有fork警告）；日志/tmp/sam-u-empty-final-pytest.log。compileall及diff-check单独exit0。此数字取代本轮提交基线1525作为当前代码回归结果，仍不等于原件验收PASS。

追加本地修复提交后，将在原临时库同一u-original job显式恢复，保留旧16请求、原指纹/断点/审计。明确累计phase上限classify114/attribute228/extract342/plan114/leaf342（归属允许每窗初次+一次纠正，旧失败也计费），明确给新30分钟期限。不得清零账单或重处理已成功窗口。最终原件结论另记。

## 原件第二轮失败与连续授权、空叶回执修复（追加）

同库同job续跑新增73次请求，累计89次；cursor17、1叶/2修订/32事实，仍FAIL。窗口17授权46..47与48..52均已读取，员工选择46..52却被旧接口误判越界，原因是后端要求落在单一ranges段。另有早期员工no_content被误当普通写稿，以及日期格式改写拒稿。原失败与账单全保留。

CRIS新增逐行授权并集核验（真缺口/类型/大跨度仍拒）、无写入no_content回执及仅报错定位的数字诊断，fact-leaf0.5.3；旧检查点子范围complete_coordinate重新计算，原普通写稿CAS/出处/数字判据不变。详见facts-tree-grant-receipt-repair-report.md。

饭团正常WSL当前全量：**1556 passed / 1 skipped / 0 failed，177.27s**，8个既有fork警告；日志/tmp/sam-u-grants-final-pytest.log。compileall及git diff --check exit0。此为代码回归结果，原件尚未通过。

下一次仍在同一原库u-original显式resume_unresolved并续期1800秒，累计phase上限classify114/attribute228/extract342/plan228/leaf342。plan预算包含先前失败调用，不清零、不重读成功窗口。未解决归叶、冲突待办、无内容回执分别计数。
