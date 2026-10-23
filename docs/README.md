# SAM-APP 文档导航

> 修订时间：2026-10-22T22:38+08:00（Asia/Shanghai，UTC+08:00）。

现行产品设计的权威位置是独立的SAM-DOCS仓库。本目录保留与代码一起使用的运行说明，以及已有设计、研究和施工记录；历史验收数字仅对应原记录的版本和时间。

## 运行与代码说明

- [本地记忆](local-memory.md)：记忆层运行与配置说明。
- [资料生命周期](data-lifecycle.md)：数据与隔离约定。
- [主页与原型接线](home-shell.md)：页面和原型的衔接记录。
- [云端主页](cloud-home.md)：云端页面相关说明。
- [Web服务入口](../webapp/README.md)。

具体命令及状态仍须对照当前代码和仓库根目录AGENTS.md。

## 历史资料

| 位置 | 内容 |
|---|---|
| [architecture/](architecture/) | 历史架构、功能交接与设计材料 |
| [product/](product/) | 早期产品思考 |
| [research/](research/) | 竞品和技术研究 |
| [handoffs/](handoffs/) | 施工回执与复核记录 |
| [DESIGN.md](DESIGN.md) | 早期资料柜与待办中心设计说明 |
| [M5施工汇报汇编](handoffs/2026-10-13-m5-delivery-history.md) | 原根目录report.md，正文完整保留；历史文件清单中的report.md指原位置 |

## 放置约定

启动与维护脚本放scripts/，隔离环境工具放tools/，测试与夹具放tests/；施工回执放docs/handoffs/，使用明确日期或任务名称，不在根目录累积report.md。新的产品方案、审批和阶段进度在SAM-DOCS维护，避免两处各写一套现行设计。

根目录index.html承担在线原型跳转，prototype/保存原型；data/中的运行文件按.gitignore隔离，受版本管理的样例保留。
