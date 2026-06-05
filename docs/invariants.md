# Agent libOS Runtime Invariants

This document maps the current runtime safety invariants to regression tests.
It is the submission-facing checklist for keeping the implementation, README,
and paper claims aligned.

## Authority Boundary

| Invariant | Current regression coverage |
| --- | --- |
| Tool visibility is not resource authority. A visible tool still fails at the primitive boundary without the required resource capability. | `tests/test_demo_contract.py::DemoContractTests::test_tool_outside_process_tool_table_is_denied_without_human_approval`, `tests/test_external_boundaries.py::ExternalBoundaryTests::test_read_file_tool_cannot_bypass_filesystem_capability`, `tests/test_external_boundaries.py::ExternalBoundaryTests::test_write_file_tool_cannot_bypass_filesystem_capability` |
| A process can call only tools in its process tool table. | `tests/test_external_boundaries.py::ExternalBoundaryTests::test_process_cannot_call_tool_outside_creation_tool_table`, `tests/test_llm_context_memory.py::LLMContextMemoryTests::test_llm_prompt_lists_only_process_visible_tools` |
| LLM-facing tools are wrappers over primitives and must not directly touch host filesystem, terminal, network, shell, database, or secrets. | `tests/test_stage2_security.py::Stage2SecurityTests::test_builtin_tools_do_not_directly_touch_host_boundaries`, `tests/test_resource_substrate.py` |
| Primitive checks happen at point of use, before protected side effects. | `tests/test_filesystem_directory_tools.py::FilesystemDirectoryToolTests::test_filesystem_primitive_enforces_read_limits_without_tool_schema`, `tests/test_shell_primitive.py::ShellPrimitiveTests::test_shell_primitive_enforces_timeout_limit_without_tool_schema`, `tests/test_permission_policy.py::PermissionPolicyTests::test_write_preconditions_fail_before_per_use_prompt` |
| Skill visibility is not resource authority. Loading a Skill can add tools, JIT tools, and prompt instructions, but declared `required_capabilities` are advisory and never auto-granted. | `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_loaded_existing_tool_visibility_does_not_grant_resource_authority`, `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_loaded_skill_instructions_are_materialized_into_llm_prompt_and_persisted_calls` |
| Skill registration and loading are primitive operations controlled by Skill/source capabilities, global manifest trust, workspace filesystem reads, human approval, and audit. | `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_skill_manifest_validation_and_global_trust`, `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_workspace_load_reads_via_filesystem_and_uses_human_once_for_skill_authority`, `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_skill_syscall_load_yaml_uses_primitive_capabilities_not_tool_table` |

## Process And Capability Semantics

| Invariant | Current regression coverage |
| --- | --- |
| fork/spawn do not implicitly inherit broad external authority. | `tests/test_child_process_tools.py::ChildProcessToolTests::test_spawn_child_process_creates_fresh_child_without_parent_memory_or_default_caps`, `tests/test_external_boundaries.py::ExternalBoundaryTests::test_fork_does_not_inherit_parent_filesystem_write_capability` |
| Child processes inherit only explicit, parent-held capability subsets. | `tests/test_child_process_tools.py::ChildProcessToolTests::test_spawn_child_process_inherits_only_explicit_capabilities`, `tests/test_granular_permissions.py::GranularPermissionTests::test_child_cannot_inherit_broader_permission_than_parent_has` |
| exec replaces the process image/tool table without granting target-image capabilities. | `tests/test_child_process_tools.py::ChildProcessToolTests::test_exec_process_swaps_image_without_granting_target_image_capabilities` |
| Revocation and one-shot capabilities are enforced at the next primitive use. | `tests/test_external_boundaries.py::ExternalBoundaryTests::test_revoked_filesystem_capability_denies_write`, `tests/test_permission_policy.py::PermissionPolicyTests::test_missing_delete_consumes_one_time_grant` |
| Process cwd is process-local; relative filesystem and shell paths resolve from that cwd without changing host process cwd. | `tests/test_process_working_directory.py::ProcessWorkingDirectoryTests::test_filesystem_tools_resolve_paths_from_process_working_directory`, `tests/test_process_working_directory.py::ProcessWorkingDirectoryTests::test_shell_tool_runs_from_process_working_directory`, `tests/test_coding_agent_launcher.py::CodingAgentLauncherTests::test_launcher_does_not_change_host_working_directory` |

## Object Memory And Context

| Invariant | Current regression coverage |
| --- | --- |
| Bare Object Memory names resolve in the caller process namespace. | `tests/test_object_memory.py::ObjectMemoryNameTests::test_same_bare_name_is_isolated_between_process_namespaces` |
| Object and namespace names are not capabilities. | `tests/test_object_memory.py::ObjectMemoryNameTests::test_name_lookup_does_not_bypass_object_capability`, `tests/test_object_memory.py::ObjectMemoryNameTests::test_namespace_write_and_list_rights_are_enforced` |
| Same local object name can exist independently across namespaces. | `tests/test_object_memory.py::ObjectMemoryNameTests::test_same_local_name_is_allowed_in_process_and_explicit_namespaces` |
| Object payloads are not durable ordinary Object rows; checkpoint snapshots are the explicit durable payload exception. | `tests/test_object_memory.py::ObjectMemoryNameTests::test_object_payload_is_not_written_to_sqlite`, `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_restore_recovers_process_subtree_objects_capabilities_and_cwd_only` |
| Large file/object transfer paths can avoid materializing full content into a process-visible tool result. | `tests/test_object_file_tools.py::ObjectFileToolTests::test_copy_file_via_named_object_without_materializing_content_to_process` |

## Human, IPC, And Scheduling

| Invariant | Current regression coverage |
| --- | --- |
| Human approval is a blocking runtime operation that resumes the pending action after a decision. | `tests/test_permission_policy.py::PermissionPolicyTests::test_llm_pending_per_use_approval_does_not_return_action_until_decision`, `tests/test_human_question_tool.py::HumanQuestionToolTests::test_async_runtime_resumes_human_question_with_answer` |
| Human output/questions route through HumanObject and the Resource Provider Substrate. | `tests/test_resource_substrate.py::ResourceProviderSubstrateTests::test_runtime_human_primitive_uses_injected_provider`, `tests/test_external_boundaries.py::ExternalBoundaryTests::test_human_output_tool_cannot_bypass_human_capability` |
| Process messages are explicit mailbox entries, not prompt text. Interrupts preempt before non-message tool calls; normal messages notify after a tool call. | `tests/test_process_messages.py::ProcessMessageTests::test_interrupt_message_preempts_tool_call_until_read`, `tests/test_process_messages.py::ProcessMessageTests::test_normal_message_notifies_after_tool_call_without_preempting` |
| Blocking process-message receive waits inside the syscall/tool action and resumes when a matching message arrives. | `tests/test_process_messages.py::ProcessMessageTests::test_receive_process_messages_blocks_until_matching_message_then_resumes`, `tests/test_process_messages.py::ProcessMessageTests::test_receive_message_syscall_waits_inside_single_syscall_until_matching_message`, `tests/test_process_messages.py::ProcessMessageTests::test_receive_message_syscall_blocks_by_default` |
| Async sleep and waiting processes do not block unrelated runnable processes. | `tests/test_async_scheduler.py::AsyncSchedulerTests::test_two_processes_alternate_time_output_via_async_sleep`, `tests/test_async_scheduler.py::AsyncSchedulerTests::test_async_runtime_drains_human_queue_and_resumes_pending_permission_action` |

## Shell And JIT Containment

| Invariant | Current regression coverage |
| --- | --- |
| Shell execution uses argv tokens, not implicit shell strings; unsafe command strings cannot bypass policy matching. | `tests/test_shell_primitive.py::ShellMatcherTests::test_custom_exact_whitelist_rule_does_not_prefix_match_extra_args`, `tests/test_shell_primitive.py::ShellPrimitiveTests::test_blacklist_policy_asks_for_nested_shell_interpreter`, `tests/test_stage2_security.py::Stage2SecurityTests::test_shell_syscall_rejects_non_list_argv_before_policy` |
| Shell allow/block policies enforce always-deny, allowlist, blocklist, per-use approval, timeout, and output bounds. | `tests/test_shell_primitive.py` |
| Deno/TypeScript JIT tools are process-local and cannot shadow static tools. | `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_tool_is_visible_only_to_registering_process`, `tests/test_stage2_security.py::Stage2SecurityTests::test_jit_tool_names_are_process_local`, `tests/test_stage2_security.py::Stage2SecurityTests::test_jit_tool_cannot_shadow_existing_tool_name` |
| JIT syscalls bypass the LLM-facing tool table but still require primitive capabilities and policy approval. | `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_syscall_bypasses_tool_table_but_not_capabilities`, `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_syscall_denies_missing_capability`, `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_human_approval_is_internal_to_syscall` |
| Deno tools do not receive ambient host permissions or unsafe imports. | `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_static_check_rejects_unsafe_typescript`, `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_static_check_import_allowlist`, `tests/test_stage2_security.py::Stage2SecurityTests::test_real_deno_tool_runs_and_has_no_host_read_permission` |
| JIT `process.exit` and `process.exec` are applied after the Deno tool returns its normal result. | `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_process_exit_is_applied_after_tool_result`, `tests/test_stage2_security.py::Stage2SecurityTests::test_deno_jit_process_exec_is_applied_after_tool_result` |
| JIT tools loaded from Skills use the same ToolBroker registration path, Deno sandbox checks, process-local visibility, and no-static-shadowing rules as manually proposed JIT tools. | `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_jit_skill_tool_is_process_local_and_uses_existing_deno_validation_path` |

## Persistence And Audit

| Invariant | Current regression coverage |
| --- | --- |
| Runtime metadata persists across SQLite reopen where the current MVP claims persistence. | `tests/test_persistent_runtime.py::PersistentRuntimeTests::test_static_tool_ids_survive_runtime_reopen`, `tests/test_process_working_directory.py::ProcessWorkingDirectoryTests::test_process_working_directory_persists_in_sqlite`, `tests/test_process_messages.py::ProcessMessageTests::test_process_messages_are_durable_in_sqlite` |
| Real LLM calls are persisted with prompt, visible tools, output, tool calls, token usage, reasoning metadata, raw response, and errors when available. | `tests/test_llm_context_memory.py::LLMContextMemoryTests::test_llm_call_records_persist_prompt_output_usage_and_reasoning`, `tests/test_human_llm_chat_script.py::HumanLLMChatScriptTests::test_model_responder_persists_nested_text_llm_call` |
| Checkpoint restore is scoped to a process subtree and does not delete append-only audit, event, LLM call, checkpoint, or external-effect history. | `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_restore_preserves_append_only_history_and_reports_external_effects`, `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_restore_recovers_process_subtree_objects_capabilities_and_cwd_only` |
| Checkpoint inspect, restore, fork, and syscall access are capability-controlled independently of LLM-facing tool visibility. | `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_checkpoint_capabilities_gate_inspect_restore_and_fork`, `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_checkpoint_syscalls_use_primitive_capabilities`, `tests/test_checkpoint_manager.py::CheckpointManagerTests::test_default_images_expose_only_low_risk_checkpoint_tools` |
| Checkpoint restore/fork preserves scoped Skill registry rows, loaded Skill records, JIT source metadata, and process tool tables without rolling back append-only history. | `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_checkpoint_restore_preserves_loaded_skill_records_and_tool_table`, `tests/test_checkpoint_manager.py` |
| Image registration goes through the image primitive and requires image/filesystem authority. | `tests/test_image_registration.py::ImageRegistrationTests::test_register_image_primitive_validates_tools_and_emits_audit`, `tests/test_image_registration.py::ImageRegistrationTests::test_load_image_from_yaml_tool_requires_image_write_capability` |
| Image `default_skills` load at spawn/exec, fork inherits loaded Skills and corresponding tool visibility, and spawn-child starts without parent-loaded Skills. | `tests/test_skills_dynamic_loading.py::SkillDynamicLoadingTests::test_image_default_skills_spawn_fork_spawn_child_and_exec_semantics` |
| Demo and launcher audit counts are scoped and reproducible enough for artifact smoke checks. | `tests/test_demo_contract.py::DemoContractTests::test_run_demo_returns_auditable_contract`, `tests/test_coding_agent_launcher.py::CodingAgentLauncherTests::test_audit_counts_are_scoped_to_launched_process` |

## Known Test Gaps

- Audit explain is not implemented yet; current tests check audit record emission and selected audit counts, not query/explanation completeness.
- Benchmark side-effect oracles are not implemented yet; current tests are regression tests, not paper evaluation workloads.
- Context materialization metadata is not yet complete enough to compute included/omitted/summarized/truncated object statistics for every LLM call.
- Real MCP, Git worktree, and mock PR providers are planned but not implemented.
