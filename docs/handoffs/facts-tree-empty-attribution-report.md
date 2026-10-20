# 段U原件返工：空归属回执

## 起点及真实失败（不改判据）

HEAD 38e375149e8a88970098d5e1dff62771ec182a44。本轮前饭团全量1525 passed/1 skipped仅是提交基线，不是本轮全量。原件首次FAIL保持：16请求、窗口3停于missing window attribution，partial、无叶/事实/修订/原子投影；本轮CRIS未真跑或恢复原件job。

只读本机result.json核实窗口3已有classification、ranges=None，累计calls=classify4/attribute4/extract3/plan3/leaf2。标题/模板内容是否无归属由员工判断，后端不识别标题/关键词。私有原trace/原件没有复制到其它目录，未读凭证。

## 最小改动

- app/document_read/service.py::_prepare_md_window：mapped-md-v2 attribute请求新增attribute_contract，合法空归属为`{"ranges":[],"outcome":"no_attribution","reason":"非空员工依据"}`。reason最多2000字符；非空ranges保留原entity/真实授予行范围校验，兼容已有未给outcome的非空回执。
- 空归属保存attribution_decision和ranges=[]，仍继续extract和独立plan/leaf；不自动等同no_content。无事实的定性叙述仍可按已读MD证据归叶，不造实体/事实。空ranges下非空候选仍由原_verify拒绝（没有员工授予归属范围）；module/quote/数字/来源守卫不变。
- 格式错误保存attribution_rejections（原响应+真实原因）及仅结构错误/契约的反馈，不回传坏原稿全文。每次调用先计费；每次run至多初次+一次格式纠正，且受生命周期attribute总限额约束。结构上界/总预算耗尽留partial、ranges=None、已有classification/账单/原审计；重构reader可从attribute接续，不清零或重提取已完成窗口。显式续跑仍受已有deadline/租约规则，未加新预算相。
- skills/file-processor/SKILL.md 1.2.0→1.2.1：v2空归属合法格式、无实体不猜、无事实不等同无叙述。
- skills/fact-leaf/SKILL.md 0.5.1→0.5.2：read_assign由员工判断纯标题/问题模板无具体内容时no_content+原因；不填空叶；定性无facts不忽略；重复确认可no-op，既有空叶保护不动。仅员工手册规定语义，不是宿主分类规则。
- 官方build_phase1_catalog.py重生成9技能，rejected=[]；既有8 tools/rejected_tools=1不变，tool-registry零diff。test_phase1_catalog同步两个正式版本。
- 新增tests/test_document_read_empty_attribution.py（9实例）：真实LocalVectorMemoryProvider/隔离SQLite/离线员工；标题及模板空归属后继续下一窗口、定性叙述无facts（含叙述本身也空归属）、4类空回执/shape纠正、1或2次限额partial重构续跑、空归属伪造事实拒绝及零原子卡。

没有新schema/迁移，没有改保留旧块/CAS/发布事务/原数字原文范围护栏；关闭态service旧分支/test_document_read.py/golden未改。

## 实际离线数字

- 新9例+catalog4例首次：13 passed，2.14s。
- 新专项、U2并行、U1映射、U3消费、关闭态document_read、catalog六文件相关回归：**120 passed，20.49s，0 failed**。
- 最后补齐fact-leaf read_assign手册并官方再生成catalog后，新9例+catalog4例：**13 passed，2.15s，0 failed**。
- `.venv/bin/python -m compileall -q app webapp tests scripts`：exit 0。
- `git diff --check`：exit 0。
- protected diff（app/db、provider.py、md_publish.py、test_document_read.py、关闭态golden、tool-registry）空。本次未全量；1525基线不可当本轮全量结果。

## 同一原件job恢复说明 / 边界

饭团仍在原临时库同一u-original job、同一QC绑定document指纹恢复，保留旧16请求trace、旧账单和成功窗口。窗口3ranges=None且已有classification，下一次只先发attribute；合法空回执后再extract/plan，无默认公司或后端模板识别。CRIS没有替饭团操作该真实临时库。

已失败的attribute请求也计费：114窗的正常attribute=114只是无纠错理想值，已有一次失败意味着完整覆盖至少需115次累计attribute，后续纠错可能更多。请饭团明确给所需总限额/期限；代码不偷偷扩大114或清零账单。若限额不足，继续partial而非伪done；格式纠正的每轮上限为一次，不无限重试。

本轮原件依旧未通过，不把离线120通过或上轮双员工续跑PASS当作原件通过。未真模型、未stage/commit/push/deploy，未碰harness运行脏文件。完成停手，等待饭团全量、本地追加提交及原u-original显式续跑。
