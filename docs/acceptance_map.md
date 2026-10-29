# P0 验收映射

本表对应保留的[实现规格](specs/reference/RSI_Safety_Lab_Codex_Implementation_Prompt.md)第 21 节。它定位代码和可执行断言，不以测试数量代替实验语义。实际执行命令、结果、安装包和 CLI 证据由本阶段交接记录保存；M1–M3 的历史记录保持原样。所有执行对象仍是登记的 Fake/control fixture。

表中测试路径均相对项目根目录。一个测试可以同时验证多条不变量；参数化测试没有人为拆开凑数。

| 编号 | 验收语义 | 实现边界 | 对应测试 |
| --- | --- | --- | --- |
| 1 | 无干预仍经过四个实际检查点 | `subjects/fake.py`、`engine/runner.py` | `tests/m2/test_preparation.py::test_four_checkpoints_correspond_to_real_work_without_deployment` |
| 2 | 不支持的检查点、补丁和能力在请求前失败 | `config.py`、`wiring.py`、`preparation.py` | `tests/m2/test_verification.py::test_invalid_inputs_are_rejected_before_any_charge`；`tests/m3/test_m3_cli.py::test_m3_preflight_and_checkpoint_flag_fail_before_creating_output` |
| 3 | process 各臂保留各自生成物，不以相同 hash 配对 | `studies/preparation.py`、`studies/s0.py` | `tests/m2/test_preparation.py::test_actual_q_requests_produce_independently_evaluated_candidates`；`tests/m4/test_s0.py::test_q_pairs_bind_distinct_contexts_and_freeze_mechanisms` |
| 4 | fixed_candidate 首轮不重生候选，来源和共享费用独立 | `studies/preparation.py`、`subjects/fake.py` | `tests/m2/test_studies.py::test_each_fixed_arm_references_shared_generation`；`tests/m4/test_s0.py::test_fixed_pool_first_round_is_shared_then_continues_by_generation` |
| 5 | 原生选择为空时 not_applicable，不伪造 accept | `engine/commit.py` | `tests/m3/test_commit.py::test_not_applicable_has_no_fabricated_decision` |
| 6 | 注册尝试数、提交数和暴露数是不同计数 | `studies/s0.py`、`analysis/export.py` | `tests/m4/test_s0.py::test_budget_exhaustion_keeps_all_registered_slots`；`test_horizon_preregistered_before_dispatch_and_resume_uses_same_slots` |
| 7 | q 补丁只变实际上下文，不改代码、经验原文或测试实现 | `interventions/context.py` | `tests/m2/test_context.py::test_five_frozen_interventions_feed_actual_bytes` |
| 8 | Restore 改变实际输入并改变后续候选 | `infra/models.py`、`subjects/fake.py` | `tests/m2/test_preparation.py::test_actual_q_requests_produce_independently_evaluated_candidates` |
| 9 | 恢复目标截断不能继续生成或旁路读取 | `interventions/context.py`、`engine/runner.py` | `tests/m2/test_context.py::test_b15_does_not_raise_budget_or_hide_partial_bytes`；`tests/m2/test_studies.py::test_restore_truncation_blocks_model_dispatch_in_study` |
| 10 | Restore/Placebo 匹配实际字节与单位 | `interventions/context.py` | `tests/m2/test_context.py::test_placebo_requires_exact_bytes_count_even_if_units_match`；`test_missing_neutral_entry_is_invalid_without_ad_hoc_filler` |
| 11 | v 固定池不变，执行与选择可不同 | `subjects/verification.py` | `tests/m2/test_verification.py::test_frozen_four_plans_execute_and_choose_expected_candidate`；`tests/m4/test_s0.py::test_fixed_pool_first_round_is_shared_then_continues_by_generation` |
| 12 | 超时和未执行不能算通过或覆盖 | `subjects/verification.py` | `tests/m2/test_verification.py::test_per_candidate_budget_does_not_turn_unrun_tests_into_pass`；`test_unknown_does_not_pass_and_keeps_reservation` |
| 13 | 费用与真实派发/回执一致 | `infra/journal.py`、`subjects/verification.py` | `tests/m2/test_verification.py::test_branch_budget_actual_cost_and_exposure_follow_dispatch`；`tests/m1/test_journal.py::test_actual_overrun_remains_chargeable_and_blocks_further_work` |
| 14 | 分支环境、记忆、游标及随机流隔离 | `engine/state.py`、`infra/snapshots.py`、`randomness.py` | `tests/m1/test_state.py::test_branch_worlds_are_isolated_from_sibling_and_snapshot`；`tests/m1/test_contracts.py::test_random_streams_pair_by_position_without_shared_mutable_rng`；`tests/m3/test_probes.py::test_real_descendants_replace_e_with_d_and_leave_main_unchanged` |
| 15 | 拒绝保留代码，三种经验恢复各自生效 | `engine/commit.py` | `tests/m3/test_commit.py::test_rejection_keeps_code_preserves_preparation_and_recovers_new_memory` |
| 16 | 暂停不删费用/暴露，未运行尝试仍在 H 中 | `engine/execution.py`、`studies/s0.py` | `tests/m3/test_execution.py::test_unsafe_current_pauses_from_deployed_log_and_resume_is_read_only`；`tests/m4/test_s0.py::test_horizon_preregistered_before_dispatch_and_resume_uses_same_slots` |
| 17 | 四指针与两个实际回执分开保存、导出 | `studies/transition.py`、`analysis/export.py` | `tests/m3/test_transition.py::test_exact_frozen_transition_and_idempotent_replay`；`tests/m4/test_export.py::test_transition_export_preserves_four_pointers_and_shared_charge_once` |
| 18 | 候选、来源、基座、协议或 head 失配拒绝 | `engine/commit.py` | `tests/m3/test_commit.py::test_decision_rejects_wrong_candidate_base_protocol_or_evidence`；`test_persisted_selection_must_match_original_occurrence_provenance`；`test_stale_revision_cannot_commit` |
| 19 | adapter 偷换 staging 状态由控制器复核拦截 | `subjects/materialize.py`、`engine/commit.py` | `tests/m3/test_commit.py::test_actual_staging_is_rechecked_before_commit`；`test_worker_cannot_change_registered_target_or_candidate` |
| 20 | probe 可推进自身，不能授权主分支 | `auditing/probes.py`、`engine/commit.py` | `tests/m3/test_probes.py::test_real_descendants_replace_e_with_d_and_leave_main_unchanged`；`tests/m3/test_commit.py::test_role_and_explicit_allowlist_both_required` |
| 21 | 字节同一不等于来源同一，不因 probe 出现同 hash 拒绝原候选 | `contracts.py`、`engine/commit.py` | `tests/m1/test_state.py::test_snapshot_graph_separates_content_occurrence_and_binding`；`tests/m3/test_m3_cli.py::test_m3_cli_run_resume_inspect` 的 probe 配置（原候选和安全后代同字节仍接受） |
| 22 | 重复提交不重复转换状态 | `engine/commit.py` | `tests/m3/test_commit.py::test_rejection_keeps_code_preserves_preparation_and_recovers_new_memory`；`test_old_commit_replay_preserves_later_world_effects` |
| 23 | preparing 中断按持久游标继续 | `engine/runner.py`、`engine/state.py` | `tests/m2/test_preparation.py::test_stage_response_can_replay_after_missing_controller_checkpoint`；`tests/m1/test_state.py::test_cursor_can_be_reopened_by_fresh_python_process` |
| 24 | 审计中断复用已完成 probe | `auditing/probes.py` | `tests/m3/test_probes.py::test_process_kill_after_probe_branch_recovers_without_repeating_cost` |
| 25 | staging 后未 commit，主 head 不晋级 | `engine/commit.py` | `tests/m3/test_transition.py::test_kill_and_new_process_recovery_matches_unchanged_golden` 的 after_staging 点 |
| 26 | commit 后响应丢失恢复原收据 | `engine/commit.py` | `tests/m3/test_commit.py::test_after_commit_recovery_finishes_workspace_without_recommitting`；`tests/m3/test_transition.py::test_kill_and_new_process_recovery_matches_unchanged_golden` 的 after_commit 点 |
| 27 | 同回执不重复计费，新重试另记 | `infra/journal.py` | `tests/m1/test_journal.py::test_reservation_and_receipt_replay_survive_reopen`；`test_unknown_retains_budget_until_reconciliation_and_retry_is_new` |
| 28 | unknown 保留预留，不变免费失败/安全通过 | `infra/journal.py`、`subjects/fake.py` | `tests/m3/test_execution.py::test_unknown_generation_keeps_reservation_and_cannot_authorize_commit`；`tests/m3/test_probes.py::test_confirmed_violation_wins_over_later_timeout` |
| 29 | 预算不足明确缺失，已知风险不消失 | `auditing/baselines.py`、`analysis/metrics.py` | `tests/m3/test_probes.py::test_incomplete_probe_budget_never_accepts`；`test_confirmed_violation_wins_over_later_timeout`；`tests/m4/test_metrics.py::test_confirmed_harm_after_legal_completion_does_not_hide_known_utility` |
| 30 | AdmissionView 显式构造、嵌套引用不授予封存读取能力 | `lifecycle.py`、`auditing/baselines.py`、`infra/views.py` | `tests/m3/test_probes.py::test_native_and_reject_all_use_bound_write_only_evidence`；`tests/m1/test_state.py::test_view_does_not_grant_access_merely_from_digest_knowledge`；`tests/m4/test_views.py::test_admission_view_rejects_nested_sealed_fields` |
| 31 | ProbeSession 不提供主写入或通用读取句柄 | `auditing/probes.py` | `tests/m3/test_probes.py::test_real_descendants_replace_e_with_d_and_leave_main_unchanged`；`test_probe_target_and_budget_are_frozen_on_replay` |
| 32 | 训练导出不读封存库，决策不充当标签 | `analysis/access.py`、`analysis/export.py`、`analysis/metrics.py` | `tests/m4/test_access.py::test_training_partition_rejection_happens_before_any_injected_read`；`tests/m4/test_metrics.py::test_decisions_do_not_create_counterfactual_labels_or_count_missing_as_accept`；`tests/m4/test_access.py::test_real_training_export_uses_poisoned_database_and_artifact_ports`、`test_real_training_export_rejects_sealed_before_opening_any_port` |
| 33 | 同历史、前缀、完整克隆不跨分区 | `analysis/partitions.py` | `tests/m4/test_partitions.py::test_history_prefix_and_root_members_share_declared_partition`；`test_exact_full_state_candidate_protocol_clone_cannot_cross_partition`；`test_existing_declaration_wins_and_later_clone_bridge_fails_visibly` |
| 34 | 已知违规与缺失分开，缺失不填零 | `oracles/workflow.py`、`analysis/metrics.py` | `tests/m2/test_workflow.py::test_unknown_and_unrun_results_stay_missing`；`tests/m3/test_execution.py::test_logged_unsafe_effect_still_pauses_when_response_is_unknown`；`tests/m4/test_metrics.py::test_wrong_denominator_and_tampered_risk_or_ratio_are_rejected` |
| 35 | 协议解析/摘要稳定，resume 不能改协议 | `serialization.py`、`config.py`、各 study loader | `tests/m1/test_contracts.py::test_utf8_sorted_objects_preserve_array_order`；`tests/m1/test_cli.py::test_resume_rejects_changed_config_and_protocol`；`tests/m4/test_s0.py::test_changed_stored_config_is_not_silently_resumed` |
| 36 | 不具备硬隔离时拒绝任意生成代码 | `wiring.py` | `tests/m1/test_cli.py::test_capability_preflight_never_silently_falls_back`；`tests/m3/test_m3_cli.py::test_m3_preflight_and_checkpoint_flag_fail_before_creating_output` |

## 补充冻结要求

| 冻结断言 | 测试 |
| --- | --- |
| 附录 A 的 D/P/N/E × observe/enforce 逐任务实际效果 | `tests/m2/test_workflow.py::test_frozen_program_results_from_actual_effects` |
| q 的固定字节、摘要、零容差、缺目标与 B=15 截断 | `tests/m2/test_context.py::test_five_frozen_interventions_feed_actual_bytes`、`test_new_context_hash_matches_literal_golden`、`test_b15_does_not_raise_budget_or_hide_partial_bytes` |
| v 必须从 pre_verification；已有固定候选不变 | `tests/m1/test_cli.py::test_capability_preflight_never_silently_falls_back`；`tests/m2/test_studies.py::test_bad_m2_config_fails_before_output_creation`；`tests/m4/test_s0.py::test_fixed_pool_first_round_is_shared_then_continues_by_generation` |
| probe 三值真值表；已违规优先于另一分支 unknown | `tests/m3/test_probes.py::test_reject_priority_and_missing_never_means_accept`、`test_confirmed_violation_wins_over_later_timeout` |
| 暂停只来自实际主部署效果，候选和 probe 不能触发 | `tests/m3/test_execution.py::test_rejected_unsafe_candidate_does_not_pause_safe_deployed_version`；`tests/m3/test_probes.py::test_confirmed_violation_wins_over_later_timeout`；`tests/m4/test_views.py::test_sealed_data_cannot_change_admission_or_pause` |
| `[1,0,null]` 的 H=3、m=2、完整均值 null、观测均值 1/2、逻辑界 [1/3,2/3] | `tests/m4/test_metrics.py::test_frozen_risk_vector_has_missing_primary_mean_and_logical_bounds` |
| 附录 B 原始轨迹、共享总费用、下一轮检索与进程恢复 | `tests/m3/test_transition.py::test_exact_frozen_transition_and_idempotent_replay`、`test_kill_and_new_process_recovery_matches_unchanged_golden`；`tests/m1/test_golden.py::test_manifest_tracks_preserved_sources_and_frozen_bytes` |
| WAL、只读事务和读取端口白名单 | `tests/m1/test_snapshots.py::test_backup_includes_committed_but_uncheckpointed_wal`；`tests/m4/test_access.py::test_readonly_database_preserves_committed_wal_and_refuses_mutation`、`test_read_transaction_remains_consistent_while_writer_appends`、`test_explicit_reader_and_opener_are_used_but_nested_ref_is_not_authorized` |

## 完整导出链补充映射

| 断言 | 真实调用链测试 |
| --- | --- |
| process 不按候选 hash 删行、H 不缩短、缺失不填零、资源与回执保留 | `tests/m4/test_export.py::test_paired_export_keeps_different_candidates_missing_horizon_and_costs` |
| 附录 B 四指针分别导出，共享 5 只收费一次、L/R 各 1 | `tests/m4/test_export.py::test_transition_export_preserves_four_pointers_and_shared_charge_once` |
| 费用归属、部署目标和 null→0 篡改不能导出 | `tests/m4/test_export.py::test_export_rejects_corrupt_fact_bindings` |
| suite checkpoint 未落盘前已记录事故，仍保留已知 risk=1 | `tests/m4/test_export.py::test_export_retains_confirmed_incident_before_suite_checkpoint` |
| 训练导出特征不包含准入决定，未测量候选标签为 null | `tests/m4/test_export.py::test_training_is_features_with_explicit_unmeasured_counterfactual` |
| 真实 export_run 注入禁止封存数据库/表/工件读取的端口，检查输出引用白名单 | `tests/m4/test_access.py::test_real_training_export_uses_poisoned_database_and_artifact_ports` |
| sealed 来源在任何注入端口打开前拒绝；混合分区在准备工件读取前拒绝 | `tests/m4/test_access.py::test_real_training_export_rejects_sealed_before_opening_any_port`、`test_mixed_branch_partition_is_rejected_before_preparation_artifact_reads` |
| AdmissionView 本体、binding、候选来源及嵌套 ArtifactRef 拒绝额外 sealed 字段 | `tests/m4/test_views.py::test_admission_view_rejects_nested_sealed_fields` |
| 从相同持久生成响应分叉，封存库/表/CAS 标签分别改为 0 与 1，同时在实际读取端口禁止访问；准入、实际风险、暂停和费用完全相同 | `tests/m4/test_views.py::test_sealed_data_cannot_change_admission_or_pause` |

OS 硬隔离和真实模型不属于已验证能力。没有将只检查接口属性的测试标成隔离验收，也没有把 API/GPU 条件缺失记成核心测试通过。研究层的独立候选续演尚未实现，错误拒绝率必须为 null，训练导出不虚构监督标签。
