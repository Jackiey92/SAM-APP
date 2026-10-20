# 本地 SAM 记忆（M4）

2026-10-12：代码摘除完成；真机、独立断网与 Gate 留待 M5。

## 启动

`scripts/run.sh` 保留 Node >=24、模型凭证检查及 runtime prepare → Flask 顺序；不启动或探测任何记忆后台服务。记忆底座默认 localvector，无需旧根环境变量。遗留 `SAM_MEMORY_BACKEND=ov` 在本地启动预检/装配时显式失败；`SAM_CLOUD_READONLY=1` 优先 NullMemoryProvider，读空/写拒绝，仍不需 Node/模型凭证。

事实账本与派生投影使用 RuntimeConfig.main_db（可由 SAM_MAIN_DB 注入）。默认离线 BGE 缓存位于 poc/memory_m0/model-cache，模型由部署预装，运行时不下载、不远程回退。纯工具地图导航采用惰性装配，不为导航加载模型或创建库。

## 逻辑地址与隔离

- sam://facts/<company>/...：事实正本由账本权威链写，向量是可重建投影；guide/brief 文档沿用既有写规则。KV 开口不能直写事实卡。
- sam://original/<company>/...：原件/坐标不变，外置 bin/map 不进向量。
- sam://kv/co/<company>/<domain>/...：domain 仅 cap_table、derivation、conversations。由宿主 scoped(company) 绑定；无绑定或 URI/metadata 公司不符立即 ValueError，不允许跨公司访问。对话正文有公司绑定，旧全局 thread_index 收口为每公司 conversations/thread_index.json。
- sam://kv/rt/<session>/<task>/...：会话运行态，按精确 session/task 地址读写/清理，不作为公司事实或模块召回，生产公司知识库不全局枚举 runtime。
- sam://session：预留，未启用。

paths.py 是地址构造/校验单点。KV 原样保存 JSON/JSONL 文本与 metadata，独立 memory_kv 表；put/read（get 语义）/delete 不触发 embedding、不入 m1_chunks。投影重建/删除与 KV 互不影响。L0 对话摘要在 KV 中使用 JSON summary 字段，保留对话工具返回文本的契约。

## 缺失与测试

旧根不兼容读取、不做搬迁。新地址缺失：股权 events 空/无 snapshot；对话及线程列表空；runtime events 空，精确状态 get_runtime 报 KeyError，由调用方空态处理。连接器错误仍不能冒充空结果；损坏股权链仍提供 warnings。

全量 `.venv/bin/python -m pytest -q`；编译 `.venv/bin/python -m compileall -q app scripts webapp`；`git diff --check`。新增合成主体仅谷斗科技（上海）有限公司；无真实底稿。

物理旧数据/配置/工具/进程均未清理，M4-B 必须另行授权；不要删除后台状态、原件、SAM BGE 缓存或 parser 依赖。
