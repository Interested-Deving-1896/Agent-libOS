# Agent libOS 未来工作计划

本文档用于审查和持续修改 Agent libOS 的后续工作。阶段 1 的 `coding-agent` demo 合同化已经完成；阶段 2 已将工具调用模型收敛为“进程创建时确定 tool table，tool call 本身总是允许，安全边界位于 libOS primitive”。当前已补入 `request_permission` tool 与 human permission policy 队列处理，下一优先级转向 **LLM executor 稳定性与上下文管理**，同时继续保持 capability、JIT tool scope、human queue 等安全边界的回归测试。

## 当前基线

已完成的核心能力：

- Agent Process 生命周期：`spawn`、`fork`、`exec`、`wait`、`signal`、`pause`、`resume`、`exit`。
- Object Memory：typed object、全局唯一 object name、按 name 的 capability-checked 访问、volatile runtime payload heap、进程退出自动释放其拥有的内存、object handle、object graph、MemoryView、context materialization、snapshot、merge。SQLite 只保存对象目录元数据，不保存 payload 内容。
- Capability：对象访问、工具执行、外部资源访问的 capability 检查、授权和撤销。
- ToolBroker：工具注册、进程 tool table 可见性检查、工具调用、结果对象写入、event/audit 记录。
- HumanObject：queued query、approve、reject、interrupt、output 原语，以及终端顺序处理队列。
- Filesystem primitive：workspace containment、filesystem capability 检查、`always_allow`/`always_deny`/`ask_each_time` 权限策略、event/audit 记录。
- Permission request：`request_permission` tool 包装 human permission-request 原语；`ask_each_time` 会在对应 primitive 使用时自动发起逐次人工确认，一次性 grant 成功使用后即撤销。
- Object/File bridge：`create_object_from_file` 与 `write_object_to_file` 支持文件内容在 filesystem primitive 和 Object Memory 之间流动，tool result 不向进程返回具体内容。
- LLM executor：OpenAI-compatible chat completions、tool call 执行、fallback JSON action parser。
- SQLite store：process、object、link、capability、event、audit、human request、tool、candidate、checkpoint 状态。
- CLI 与脚本：`agent-libos demo`、LLM 调度入口、文档总结脚本、真实模型写文件 smoke test。
- Demo 主路径：root process、worker fork、JIT parser、checkpoint、human approval、文件写入、最终报告对象和 audit trace。
- Demo 合同化：CLI JSON 和最终报告对象包含工具序列、filesystem capability denial before grant、human filesystem approval、write result、target file check、audit summary。
- 工具调用模型：进程创建时由 AgentImage/tool table 决定可见工具；tool call 对可见工具总是允许，外部安全边界在 OS primitive。
- 边界测试：验证工具不能绕过 filesystem/human capability、path containment、revoked capability、fork attenuation 和 JIT process scope。

当前主要不足：

- 测试覆盖仍薄，很多核心路径缺少回归测试，尤其是 process/memory/checkpoint/JIT/LLM executor。
- 外部对象适配器不完整：shell/git 是很薄的本地 adapter，browser/database 仍是占位。
- JIT sandbox 仍是 MVP 级别，不能作为生产安全边界。
- `ToolPolicy.requires_confirmation` 等字段目前是 metadata，不作为阶段 2 的安全边界。
- PolicyEngine、checkpoint/rollback、side-effect compensation、quota 等机制尚未成熟；checkpoint 只能恢复 runtime store，不能补偿已发生的外部副作用。
- Tool result 和上下文管理容易膨胀，长文档、多轮任务和真实模型兼容性仍需要系统处理。

## 阶段 1：Demo 合同化与可审查性（已完成）

完成状态：`coding-agent` demo 已经变成稳定的审查合同：一条命令能展示 Agent Process 执行目标、读取上下文、调用工具、触发授权、产生外部副作用、记录审计、输出最终报告，并且关键结果已有测试断言。

已完成任务：

- 保留当前 demo 用户故事：“分析失败测试日志，提取失败用例，写入补丁预览，输出报告”。
- `uv run agent-libos demo` 作为主入口，且不依赖真实模型。
- demo 输出包含关键对象 ID、进程 ID、工具调用序列、human approval、checkpoint、最终报告对象、写文件结果和 audit 数量。
- `run_demo()` 有集成测试，断言 root/worker pid、checkpoint、approval request、final report oid、audit count、写入文件路径和写入内容。
- 最终报告对象包含问题摘要、证据、执行过的工具、授权记录、外部副作用、checkpoint、目标文件检查和后续建议。
- demo 显式展示 tool table 与 filesystem write capability 彼此独立。
- demo 在缺失 filesystem write capability 时由 filesystem primitive 拒绝，经 human queue 批准并授予 filesystem capability 后才写入文件。
- README 与 `plan.md` 已说明 demo 能力和限制，避免把 demo 误解为生产级自动修复系统。

已验证验收标准：

- `uv run agent-libos demo` 可以稳定跑通，不依赖真实模型，不需要人工手动补步骤。
- demo 中写文件必须先具备进程 tool table 中的 `write_text_file`，并在 filesystem primitive 层具备 write capability。
- demo 对 `write_result.ok`、目标文件存在、目标文件内容、final report payload 进行断言，避免“报告成功但副作用失败”。
- 缺失 capability 时能走明确的 human approval、primitive deny 或外部资源授权路径。
- demo 产生的所有外部副作用都有 event 和 audit record。
- 最终输出和最终报告对象能让审查者看清 Agent Process、Object Memory、ToolBroker、HumanObject、Capability、Checkpoint、Audit 的协作关系。

建议测试：

- `uv run agent-libos demo`
- `uv run agent-libos --db .agent_libos.sqlite demo`
- 检查 demo 返回 JSON 中包含 root/worker pid、checkpoint、approval request、final report oid、write result、audit count。
- `uv run python -m unittest tests.test_demo_contract -v`
- `uv run python -m unittest discover -s tests -v`

## 阶段 2：安全边界固化（已完成）

完成状态：已将“工具是 libc-like wrapper，安全边界在 libOS primitive 中”落实为可测试的 MVP 约束。进程可见工具由创建时的 AgentImage/tool table 决定；ToolBroker 不再使用 `tool:<id>` execute capability 作为安全边界；filesystem/human 等 primitive 继续执行 capability、containment、event 和 audit。

已完成任务：

- 明确 tool call 不是安全边界：进程 tool table 决定可见性，可见工具调用总是允许。
- Runtime 在 process spawn/fork 时根据 AgentImage 配置 tool table；LLM executor 只暴露当前进程 tool table 中的 schema。
- ToolBroker 对不在进程 tool table 中的调用直接 deny，不触发 human approval 或 capability grant。
- JIT tool 默认注册为 `ephemeral_process`，只写入注册进程的 tool table；无关进程不可见、不可调用。
- LLM-facing filesystem/human tools 继续只调用 `ctx.runtime.<primitive>`，不直接访问 host filesystem/terminal/network/shell/database/secret。
- HumanObject 增加 pending queue 和 terminal drain，`human_output` 入队后按队列路径交付。
- filesystem/human 边界测试扩展为 path escape、revoked capability、fork attenuation、tool table deny、JIT dangerous import、JIT scope 和静态扫描。
- permission policy 边界测试覆盖 `request_permission`、always allow、always deny、ask each time、一次性授权消费，以及拒绝后进程继续 RUNNABLE。

已验证验收标准：

- 所有 LLM-facing built-in tools 不直接调用 host filesystem、terminal、network、shell、database。
- 外部副作用必须经过 libOS primitive，并在 audit trace 中可见。
- 进程 tool table 与外部资源 capability 彼此独立，测试覆盖 tool 不可见、filesystem capability 缺失、撤销和 fork attenuation。
- `write_text_file` 等高风险工具在缺失 external capability 时不会静默执行。
- `request_permission` 是普通 tool table 成员，不绕过 primitive；human 选择始终允许、始终拒绝或逐次询问后才改变 capability policy。
- `ask_each_time` 不在脚本侧特殊处理；filesystem primitive 在 `write_text` 内自动向 human queue 发起 per-use approval。
- 高风险 capability 不会被 fork/exec 自动扩大。
- JIT tool 无法直接访问凭据或未授权网络。
- Human-facing output 通过 human queue 交付，并有 request、event 和 audit 记录。

建议测试：

- `uv run python -m unittest discover -s tests -v`
- 静态扫描 built-in tools，确认没有直接 `read_text`、`write_text`、`print`、`subprocess`、`urllib`、`socket` 等外部访问。
- 覆盖安全测试：path escape、revoked capability、fork capability attenuation、tool table deny、JIT dangerous import、JIT process scope。

## 阶段 3：LLM 执行质量提升

目标：先用 fake LLM conformance tests 固化 executor 行为，再让真实模型在多轮任务中更稳定地选择正确工具，减少重复调用和上下文膨胀，并在失败时留下可诊断状态。

主要任务：

- 优先设计 fake LLM client conformance tests，覆盖 OpenAI tool call、fallback JSON、bad args、no action、provider error、empty completion、JSON 字符串参数、thinking 空输出等情况。
- 优化 system prompt 和 user prompt，让模型明确每个 quantum 只做一个有效 action，并在目标完成后调用 `process_exit`。
- 改进 tool result 存储结构，避免同时保存大段重复 `content` 和 `result` 导致 context materialization 超预算。
- 为长文本读取引入摘要对象或分页读取策略，避免模型在看不到 tool result 时重复调用同一工具。
- 增加 LLM executor 的失败分类：无 tool call、参数验证失败、capability denied、provider error、empty completion、重复 action。
- 为真实模型 smoke test 增加可选 trace 输出，包括 prompt token 估算、工具调用序列和最终 process status。
- 支持更明确的 retry 策略：只对 transient provider error 或空 completion 重试，不对 capability denied 自动重试。

验收标准：

- fake LLM conformance tests 能稳定覆盖 executor 的主要成功和失败路径，默认 CI 不依赖真实模型。
- 文档总结脚本能稳定完成 `read_text_file -> write_text_file denied -> request_permission -> write_text_file retry -> human_output -> process_exit`；若 human 拒绝，进程继续输出未完成原因并退出。
- 写文件 smoke test 能稳定完成 `write_text_file -> process_exit`，并验证文件内容。
- 模型重复读取同一文件的情况能被减少或被 executor 明确识别。
- tool result 不再轻易因重复 payload 超出 context budget。
- LLM 执行失败时，process status、status message、audit record 足以定位原因。

建议测试：

- 使用 fake LLM client 覆盖 tool call、fallback JSON、bad args、no action、provider error、empty completion、JSON 字符串参数。
- `uv run python scripts/llm_summarize_document.py agent_libos_design_doc.md --trace`
- `uv run python scripts/llm_write_goal_smoke.py`
- 使用真实模型做小样本 smoke，但不放入默认 CI。

## 阶段 4：JIT Tool 与 Skills/Tools Layer 完善

目标：形成可验证、可注册、可审计、可回滚的工具扩展机制，让 Agent 能自扩展但不能自授权。

主要任务：

- 完善 `BaseAgentTool` 与 `ToolSpec` 的 schema/policy/metadata 对齐，明确哪些字段面向 LLM，哪些字段面向 runtime。
- 将 JIT tool 注册流水线拆清：proposal object、static validation、sandbox test、human/policy approval、registration、capability grant、LLM wrapper generation。
- 为 JIT tool 增加更严格的 sandbox backend 抽象，允许后续切换 Docker、WASM、容器或远程 sandbox。
- 为 ToolCandidate 保存 provenance、测试结果、requested capabilities、批准人和注册 scope。
- 实现 tool scope 策略：ephemeral_process、ephemeral_workspace、persistent_signed。
- 将 OpenAI tool schema 暴露改为按 process/image/scope 过滤，避免 ephemeral process tool 被无关进程看到或调用。
- 为 JIT tool 增加 scope 隔离测试：父进程、子进程、无关进程之间分别验证 tool schema 可见性、tool table 成员关系和调用结果。
- 将 Skills 与 Tools 的关系讲清并落地：Skill 影响模型理解和策略，Tool 暴露可调用动作。
- 为 tool bundle 增加按 AgentImage 加载的机制，避免所有工具默认暴露给所有进程。

验收标准：

- Agent 可以提出一个简单 parser tool，经过验证后注册为 ephemeral tool，并成功调用。
- 未通过测试或请求未授权 capability 的 JIT tool 不能注册。
- 注册后的 tool schema 能作为 OpenAI tool 暴露给 LLM executor，但只对符合 scope 和 capability 的进程可见。
- JIT tool 的 proposal、validation、registration、call 都有 audit trace。
- Tool scope 与 capability grant 一致，不能跨进程越权调用。
- 无关进程不能通过全局 schema 列表发现或调用 `ephemeral_process` JIT tool。

建议测试：

- JIT tool happy path：提议、验证、注册、调用。
- JIT tool failure path：测试失败、危险 import、请求网络、请求文件写入、schema 不合法。
- Tool scope 测试：父进程、子进程、无关进程之间的 tool schema 可见性、tool table 成员关系和实际调用结果。

## 阶段 5：工程化与可观测性

目标：让项目具备持续演进的工程基础，支持回归测试、审查、性能观察和 API 文档生成。

主要任务：

- 建立测试矩阵：unit、integration、security、LLM smoke、performance smoke。
- 增加 CI workflow，默认运行不依赖真实模型和外部网络的测试。
- 为 SQLite store 增加 schema migration 或版本检查，避免未来字段演进破坏旧数据库。
- 为 audit trace 增加查询和导出能力，支持按 pid、capability、tool、external resource、time range 过滤。
- 增加关键指标：tool call count、human approval count、capability denial count、external side effect count、context token estimate、process runtime。
- 为核心 public API 写最小 API reference，至少覆盖 Runtime、ProcessManager、ObjectMemoryManager、ToolBroker、CapabilityManager、HumanObjectManager。
- 建立开发约束文档：如何写 tool、如何写 primitive、如何加 capability、如何写安全测试。

验收标准：

- 默认 CI 能在无 `.env`、无真实模型、无外部网络的环境下通过。
- 每个安全边界修复都有对应回归测试。
- audit trace 可以支撑回答“哪个进程用哪个 capability 做了哪个外部副作用”。
- 文档能让新实现者区分 Tool、Skill、libOS primitive、Host adapter。
- 本地开发者可以通过 README 和 `plan.md` 找到下一步任务和验收方式。

建议测试：

- `uv run python -m unittest discover -s tests -v`
- `uv run python -m compileall agent_libos scripts tests`
- CI 中增加 CLI smoke：`uv run agent-libos demo`
- 后续增加性能 smoke：大对象 materialization、长 audit trace 查询、多个 process 调度。

## 优先级队列

P0：LLM executor conformance、tool result 压缩、checkpoint/rollback 测试、audit 查询。

- 用 fake LLM client 固化 executor 的成功和失败路径。
- 减少真实模型重复调用工具和看不到结果的问题。
- 改进 tool result 存储和 context materialization。
- 为 checkpoint/rollback 增加可复现测试，明确外部副作用不由 store rollback 自动补偿。
- 为 audit trace 增加按 pid、capability、tool、external resource 过滤的查询能力。

P1：外部 primitive 扩展、human queue 体验、README/plan/设计文档对齐。

- 为 shell/git/browser/database/network/secret 明确最小 primitive 和 capability namespace。
- 将 terminal human queue 做成更完整的交互入口。
- 保持 README、`plan.md`、设计文档之间的状态一致。

P2：生产 sandbox、MCP、分布式调度、多租户 policy。

- 将 JIT sandbox 从 MVP 级别升级为可替换的强隔离后端。
- 增加 MCP-compatible tool exposure。
- 设计多 worker 调度和多租户 policy，但不阻塞 MVP demo。

## 暂不做事项

- 不做完整产品 UI；现阶段以 CLI、脚本和文档为主。
- 不做全局工具市场；JIT tool 先限制为 ephemeral 和本地注册。
- 不做无人监管的高风险外部副作用；文件写入、shell、网络、凭据访问至少必须经过 capability 和 libOS primitive，side-effect policy 接入前不得把 ToolPolicy metadata 当成安全边界。
- 不做强分布式运行时；当前先保证单机 SQLite MVP 的抽象正确。
- 不为所有外部服务一次性写 adapter；优先做 demo 和安全边界需要的最小集合。

## 审查建议

审查本计划时优先关注三点：

- 阶段 1 的 demo 是否足够展示 Agent libOS 与普通 tool-calling agent 的区别。
- 阶段 2 的安全边界是否足以防止 tool 绕过 libOS primitive。
- 阶段 3 到阶段 5 的顺序是否符合当前项目最需要验证的风险。

如果计划需要调整，建议直接修改各阶段的“主要任务”和“验收标准”，并保持每个任务都有可复现的测试或审查方式。
