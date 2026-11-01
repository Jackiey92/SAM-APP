# B4 契约→测试对照矩阵（B4-3 交付）

- 契约唯一依据：SAM-DOCS `04-功能模块/F-001-公司事实树/96-附录B4-Broker正式接受与幂等冻结v2.md`（v2.3，含 §2.0.2/§2.0.4/§12，冻结 2026-11-01）。
- 测试名均可用 `pytest --collect-only -q` 核对；参数展开用 `[参数]` 标注，无参数的不挂后缀。
- 「inherited」＝ B4-0/B4-1/B4-2 已落地测试守护；「本片新增」＝ B4-3
  `tests/test_facts_tree_broker_final_gate_e2e.py`（下称 **e2e**）新增守护。
- e2e 全部真机：真 git（tmp_path 内裸仓，sha1/sha256 双格式参数化）、真 sqlite、真 flock，断网，
  仅 monkeypatch 注入竞争者/结果不明点；不 mock CAS、树门、锁本身。

## A. §4 硬验收 1–9（B4-1..3 编排器）

| 契约条 | 要点 | 守护测试 `文件::函数名[参数]` | 状态 |
|---|---|---|---|
| §4-1 | 正向接受：一次 CAS、ref H→C、镜像 in_flight/accepting→accepted、accepted_commit==C；路径白名单/物理树/业务树三类校验真实经过（间谍次序） | `test_facts_tree_broker_final_gate.py::test_first_acceptance_real_validation_order_and_idempotency`；双格式端到端：`e2e::test_e2e_first_acceptance_on_real_object_format[sha1]`、`[sha256]`（含 object_format 下传间谍、镜像==head、fact-1 首版落账、双侧 verify_integrity） | inherited＋本片新增 |
| §4-2 | 幂等回放：零 CAS/零新 attempt/零新事实；定位**首次**接受提交（后续提交仍带该 op 也返回首次）；异效果→rejected | 同条 `..._idempotency`（零计数、异效果 rejected）；定位器层 `test_facts_tree_operation_acceptance.py::test_first_acceptance_inherited_through_later_generations`；真机端到端：`e2e::test_e2e_replay_zero_cas_returns_first_accepting_commit[sha1|sha256]`（先追加一个仍带 op-1 的后继提交再回放，断言返回 C1 非新头、异效果 rejected）；多 op 账本：`e2e::test_e2e_multi_operation_ledger_full_life[sha1|sha256]` 步 3/4（C2 链上回放 op-1 仍回 C1） | inherited＋本片新增 |
| §4-3 | CAS 竞态两轮：第一轮 recycle 成对回收→第二轮新鲜构造成功；只接受一次、每轮独立 attempt、非 rebase；第二轮仍竞争→§2.0.4 终态停放、retry_wait、无第三个 attempt、停放态自动 admit 被拒 | `test_facts_tree_broker_final_gate.py::test_competition_fresh_objects_two_round_budget[1]`（成功）、`[2]`（retry_wait＋再调 rejected、无第三 attempt、新鲜对象断言非 rebase）；`test_facts_tree_broker_final_gate_reconcile.py::test_readmission_two_definite_losses_park_terminal_without_third_round`；`test_facts_tree_operation_work_store.py::test_exhausted_cas_complete_timeline_and_terminal_admission`；双格式真机：`e2e::test_e2e_one_lost_cas_then_second_round_acceptance[sha1|sha256]`；账本 op-3：`e2e::test_e2e_multi_operation_ledger_full_life[sha1|sha256]`（两 attempt、stale+accepted、C3 单亲为竞争者提交、装配基序列 [C2,H1]） | inherited＋本片新增 |
| §4-4 | lost 证据门负向：H1 已含本 op（走幂等不回收）、链不健康、effect 变化、非 FINAL_ACCEPT 门/无租约、round_index=2 回收，逐一拒绝回收、无回收 CAS、状态不变 | `test_facts_tree_broker_final_gate.py::test_loss_evidence_failures_park_unknown_without_retry[git_error|unknown_result|not_successor|corrupt_chain|accepted_elsewhere|effect_changed]`；`...::test_old_patch_and_second_round_effect_change_rejected`；`...::test_fail_closed_before_cas[lease]`；`test_facts_tree_broker_coordination.py::test_final_accept_requires_live_singleton_lease`；存储层 `test_facts_tree_operation_work_store.py::test_lost_cas_round_is_exact_integer_one[2|0|-1|True|1.0|1|None]`、`...::test_lost_cas_budget_cannot_be_bypassed_by_round_one`、`...::test_lost_cas_invalid_joint_state_has_no_writes[prepared|approval_required|reserved|unknown|accepted|rejected|stale|wrong_attempt]`、`...::test_lost_cas_invalid_metadata_cannot_write`、`...::test_lost_cas_preflight_expiries_before_and_between_rounds` | inherited |
| §4-5 | unknown 失败关闭＋占用保留；出口 A 链上收敛零第二 CAS；出口 B 旧执行者未隔离/证据不足拒绝重准入 | A：`test_facts_tree_broker_final_gate_reconcile.py::test_exit_a_converges_on_chain_acceptance_with_zero_second_cas`、`..._different_on_chain_effect_is_rejected`、`..._locator_anomaly_quarantines_without_writes`、`..._corrupted_unknown_history_rejected_without_cas[op_only_accepted|commit_mismatch|attempt_only_readmission]`；B：`..._predicate_gate_failures_keep_unknown_without_cas[missing|false|raising]`、`..._full_readmission_runs_one_complete_gate`、`..._empty_chain_proof_breakage_refused[locator|range|different_effect]`、`..._facts_must_be_head_bound_true_facts`、`..._ambiguous_cas_parks_unknown_again_without_retry`；占用保留 `test_facts_tree_operation_work_store.py::test_unknown_retains_occupation_and_readmission_pairs_again`、`test_facts_tree_broker_final_gate_reconcile.py::test_fresh_entrance_still_refuses_an_unknown_operation`；幂等收敛 `...::test_reconcile_is_idempotent_after_convergence`、`..._for_a_freshly_accepted_operation`；双格式真机：`e2e::test_e2e_unknown_exit_a_converges_from_chain[sha1|sha256]`、`e2e::test_e2e_unknown_exit_b_readmits_same_attempt[sha1|sha256]`；账本 op-4（不明但实际落盘→出口 A）：`e2e::test_e2e_multi_operation_ledger_full_life[*]` | inherited＋本片新增 |
| §4-6 | 门序失败关闭：树验失败/意图未落盘/非 admitting 态/依赖新鲜度缺失；CAS 永不发生（ref==H）、无 accepted 镜像；门内无网络/模型等待（结构封死） | `test_facts_tree_broker_final_gate.py::test_fail_closed_before_cas[tree|stale_parent|intent|lease]`、`...::test_silent_intent_rollback_cannot_reach_cas`、`...::test_post_intent_validator_refusal_stops_cas`、`...::test_initial_git_evidence_quarantines_without_writes[locator|identity|head_changed]`；依赖新鲜度：`test_facts_tree_broker_final_gate_reconcile.py::test_exit_b_facts_must_be_head_bound_true_facts`；无网络/模型等待：间谍穷举调用次序（§4-1 同条 log 精确等于七调用法，无 launcher/外部 seam）＋本片真机封网 `e2e::test_e2e_final_gate_accepts_without_network_wait`（socket 全拒仍完成接受）、`e2e::test_e2e_tree_gate_failure_closes_before_cas[sha1|sha256]`（双格式树门前置关闭、零 attempt 落盘） | inherited＋本片新增 |
| §4-7 | 锁顺序与单 Broker：反向取锁/无租约/第二 Broker fail-closed；同 op 并发最多一次 CAS | `test_facts_tree_broker_final_gate.py::test_second_broker_and_lock_order_rejected`、`...::test_concurrent_duplicate_serialized_before_git_evidence`；`test_facts_tree_broker_coordination.py::test_correct_order_joins_real_existing_operation_store`、`...::test_missing_stage_and_nested_reverse_order_fail_closed`、`...::test_caller_transaction_denied_before_lock_and_left_untouched`、`...::test_same_domain_multithread_and_multi_instance_mutual_exclusion`、`...::test_singleton_held_across_instances_and_cannot_close_in_reverse_order`、`...::test_file_lock_serializes_separate_process` | inherited |
| §4-8 | 全量 detached 禁网（bwrap --unshare-net）；compileall、git diff --check、新文件 100644 非软链；改动文件 numstat；app/ diff 仅限本片 | 全量 9190+ 由饭团 bwrap 黑盒跑（本卡不跑全量）。本片新增 15 条全部 tmp_path 本地真机（裸 git/sqlite/flock），`e2e::test_e2e_final_gate_accepts_without_network_wait` 主动封 socket；compileall/100644/numstat 在 B4-3 回执给出 | 饭团黑盒＋本回执 |
| §4-9 | 分段交付：B4-0/B4-1/B4-2 已分段停手；B4-3 只本地 commit，不 push/merge/deploy | 流程条（非测试）；本卡 `git status` 仅 tests/＋矩阵 md、app/ diff 为空 | 流程 |

## B. §2.0.2 `recycle_lost_cas_attempt` 关键不变量

| 不变量 | 守护测试 | 状态 |
|---|---|---|
| 一个 SAVEPOINT 内成对落盘，attempt 事件先/operation 事件后；中途失败无半对 | `test_facts_tree_operation_work_store.py::test_lost_cas_atomic_failure_rolls_back_both_halves`、`...::test_failed_paired_call_rolls_back_both_halves_and_keeps_caller_txn`、`...::test_half_recorded_pair_fails_closed`；编排侧成对镜像由 §4-1/§4-3 端到端断言（事件序列精确比对） | inherited |
| 精确重放（同 event_id/字节）replayed=True、不写新行；内容不符冲突 | `...::test_lost_cas_replay_conflict_fails_without_rows[head|op_detail|att_detail|half|cross_id]`、`...::test_paired_call_replays_when_both_event_ids_match`、`...::test_paired_replay_with_different_content_conflicts` | inherited |
| 状态前置：仅 in_flight＋该 attempt accepting 合法，其余全拒且无写 | `...::test_lost_cas_invalid_joint_state_has_no_writes[prepared|approval_required|reserved|unknown|accepted|rejected|stale|wrong_attempt]` | inherited |
| 不开放通用 accepting→stale 写入门；mark_attempt_stale 仍只 prepared | `...::test_lost_cas_does_not_open_generic_stale_entry[accepting|unknown]`、`...::test_stale_is_reachable_only_from_prepared_pre_flight` | inherited |
| 回收后可 record 新 attempt(base=H1)＋admit_execution；旧 attempt 留审计 | `...::test_lost_cas_two_round_reads_and_replay[reserved]`；端到端 `test_facts_tree_broker_final_gate.py::test_competition_fresh_objects_two_round_budget[1]`、`e2e::test_e2e_one_lost_cas_then_second_round_acceptance[*]`、账本 `e2e::test_e2e_multi_operation_ledger_full_life[*]` | inherited＋本片新增 |
| 两轮预算：round_index 仅精确整数 1；≥2/旁路/过期前置全拒 | `...::test_lost_cas_round_is_exact_integer_one[2|0|-1|True|1.0|1|None]`、`...::test_lost_cas_budget_cannot_be_bypassed_by_round_one`、`...::test_lost_cas_preflight_expiries_before_and_between_rounds`、`...::test_lost_cas_requires_caller_transaction` | inherited |
| accepted_commit 不得出现在回收事件；元数据非法拒写 | `...::test_lost_cas_invalid_metadata_cannot_write`（7 组，含 op/att detail 携带 accepted_commit、reason 伪造、event_id 跨行复用） | inherited |
| sha256 头与双格式 | `...::test_lost_cas_sha256_heads_and_edge_allowlists`、`...::test_accepted_commit_validated_per_attempt_object_format`；端到端 `e2e::test_e2e_one_lost_cas_then_second_round_acceptance[sha256]`、`e2e::test_e2e_multi_operation_ledger_full_life[sha256]` | inherited＋本片新增 |
| 调用方硬证据门五条（同门/租约、明确 CasResult False、H1 健康后继、H1 链无本 op、effect 不变） | 见 §4-4 行（门级负向全集）＋ §4-3 行（门全过时的正向） | inherited |
| 回收轮结果不明仍走 mark_unknown（与 lost 严格区分） | `test_facts_tree_broker_final_gate.py::test_loss_evidence_failures_park_unknown_without_retry[git_error|unknown_result]`、`test_facts_tree_broker_final_gate_reconcile.py::test_readmission_ambiguous_cas_parks_unknown_again_without_retry` | inherited |

## C. §2.0.4 预算耗尽成对终态停放

| 不变量 | 守护测试 | 状态 |
|---|---|---|
| 合法完整时间线（一次认证回收→第二轮→第二轮 lost 停放）通过全部读取/完整性校验；停放态自动 admit 被拒 | `test_facts_tree_operation_work_store.py::test_exhausted_cas_complete_timeline_and_terminal_admission`；`test_facts_tree_broker_final_gate.py::test_competition_fresh_objects_two_round_budget[2]`（retry_wait、无第三 attempt、再调 rejected）；`test_facts_tree_broker_final_gate_reconcile.py::test_readmission_two_definite_losses_park_terminal_without_third_round` | inherited |
| 写入门禁：无前置回收/错 attempt/unknown 态/错轮次/原因码/ID/头不符全拒 | `test_facts_tree_operation_work_store.py::test_exhausted_cas_write_guards[no_recovery|wrong_attempt|unknown|round|reason|ids|heads]` | inherited |
| 损坏历史九负向（无回收就停放/op 单边/att 单边/原因码/event_id/跨 attempt/跨 op/停放两次/停放后自动 admit） | `...::test_exhausted_cas_corrupted_history_rejected_by_all_reads[no_recovery|operation_only|attempt_only|reason|event_id|cross_attempt|cross_op|twice|live]`、`...::test_exhausted_cas_pair_atomic_rollback` | inherited |

## D. §12 读取侧成对完整性扩展

| 不变量 | 守护测试 | 状态 |
|---|---|---|
| §12.1 边表受限加边、注释同步；写入授权不靠边表（通用迁移入口仍走不到 accepting→stale） | `...::test_lost_cas_does_not_open_generic_stale_entry[accepting|unknown]`、`...::test_stale_is_reachable_only_from_prepared_pre_flight`、`...::test_illegal_operation_edges_are_rejected` | inherited |
| §12.2 跨全部 attempt 按事件序列配对；单边/缺半/错配/unknown 误回收/提交不一致/seq 断号/id 复用/二次回收/指纹不符/未认证/错序 全部 raise | `...::test_lost_cas_corrupted_history_fails_joint_reads[operation_only|attempt_only|two_live|unknown_stale|commit_mismatch|seq_gap|event_reuse|second_recycle|fingerprint|uncertified|wrong_order]`（覆盖 §12.5 七负向且更严）；B4-2 联合态三负向 `test_facts_tree_broker_final_gate_reconcile.py::test_corrupted_unknown_history_rejected_without_cas[*]` | inherited |
| §12.3 canonical walk 统一时间线：合法两轮轨迹、旧 stale＋unknown 混合复杂史通过；前缀至多一个 live | `...::test_lost_cas_two_round_reads_and_replay[reserved|accepting|accepted|unknown]`、`...::test_complex_legal_history_with_stale_attempt_and_unknown_cycle`、`...::test_two_live_prepared_attempts_are_rejected`、`...::test_live_attempt_following_a_waiting_prepared_attempt_rejected`；多 op 混合终态全局扫描 `e2e::test_e2e_multi_operation_ledger_full_life[*]`（op1/2 单 accepted、op3 stale+accepted、op4 unknown→accepted，终态 store.verify_integrity 全扫） | inherited＋本片新增 |
| §12.4 存储层即时统计认证回收次数（≥1 拒再回收） | `...::test_lost_cas_corrupted_history_fails_joint_reads[second_recycle]`、`...::test_lost_cas_budget_cannot_be_bypassed_by_round_one` | inherited |
| §12.5/§12.6 unknown 路径零放松、既有迁移签名零改动 | 全量邻居回归（B4-3 回执合计通过数）：`test_facts_tree_operation_work_store.py`、`test_facts_tree_broker_coordination.py`、`test_facts_tree_broker_final_gate.py`、`test_facts_tree_broker_final_gate_reconcile.py`、`test_broker_tree_gate.py`、`test_facts_tree_operation_acceptance.py` | inherited（B4-3 复跑） |

## E. 横切不变量

| 不变量 | 守护测试 | 状态 |
|---|---|---|
| 零业务规则：族名→字段映射/正则/关键词/动作枚举唯一真相源仍是 record_schema/record_tree/broker_tree_gate | `test_broker_tree_gate.py::test_invalid_snapshots_fail_closed_with_original_code`（closure1–closure4、missing_consumed、orphan_consumed、nonterminal_consumed、consumed_result_mismatch 等 20 组闭包负向码）、`test_broker_tree_gate.py::test_valid_complete_snapshots_and_context`（genesis/complete_unit/append/current_advance 各 ×sha1/sha256）；B4-3 app/ diff 为空（回执 numstat 自证），e2e 造数全部经 `SCHEMA_BUILDERS`＋`full_tree()` 派生（broker 记账记录由冻结模板机械改 ID/base/digest 绑定，无任何业务词表） | inherited＋边界自证 |
| unknown 失败关闭不重发 | §4-5 行全集；`e2e::test_e2e_unknown_exit_a_converges_from_chain[sha1|sha256]`、`e2e::test_e2e_unknown_exit_b_readmits_same_attempt[sha1|sha256]` | inherited＋本片新增 |
| 锁序仅 维护准入→接受门→工作库事务，单 Broker | §4-7 行全集 | inherited |
| 物理格式端到端（OID 40/64、effect 指纹、空链 range proof 随格式） | `e2e::test_e2e_first_acceptance_on_real_object_format[sha1|sha256]` 等六条主路径＋`e2e::test_e2e_multi_operation_ledger_full_life[sha1|sha256]`；`test_facts_tree_operation_acceptance.py::test_both_object_formats_are_supported[sha1|sha256]` | inherited＋本片新增 |
| refs facts 单调前进只经 CAS；终态 Git/镜像/首次定位三者一致；无并发 live attempt | `e2e::test_e2e_multi_operation_ledger_full_life[sha1|sha256]`：精确链序 (genesis,C1,C2,Hcomp,C3,C4)、每 op 镜像==locate 首次接受、attempt 终态仅 accepted/stale、双侧 verify_integrity | 本片新增 |

## F. 本片新增测试清单（15 条，collect-only 可对）

- `test_e2e_first_acceptance_on_real_object_format[sha1]` / `[sha256]`
- `test_e2e_replay_zero_cas_returns_first_accepting_commit[sha1]` / `[sha256]`
- `test_e2e_one_lost_cas_then_second_round_acceptance[sha1]` / `[sha256]`
- `test_e2e_unknown_exit_a_converges_from_chain[sha1]` / `[sha256]`
- `test_e2e_unknown_exit_b_readmits_same_attempt[sha1]` / `[sha256]`
- `test_e2e_tree_gate_failure_closes_before_cas[sha1]` / `[sha256]`
- `test_e2e_final_gate_accepts_without_network_wait`
- `test_e2e_multi_operation_ledger_full_life[sha1]` / `[sha256]`
