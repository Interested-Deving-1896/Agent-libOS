# Runtime Model

Agent libOS models work as `AgentProcess` instances. A process has identity,
status, a goal object, a memory view, a process-local working directory, a tool
table, loaded Skills, capabilities, children, message queue state, and resource
budgets.

The paper frames this process model as the substrate for self-evolving agents:
a process can change visible tools, activate Skills, register process-local JIT
tools, register or exec AgentImages, fork children, and fork from checkpoints,
while resource authority remains separate in Capability v2.

## Process Lifecycle

The current lifecycle includes:

- `created`: process row exists but has not started running.
- `runnable`: scheduler may run the process.
- `running`: a quantum is currently executing.
- `waiting_human`: the process is blocked on a human question or approval.
- `waiting_event`: the process is blocked on a child, message, or event.
- `waiting_tool`: reserved waiting state for tool-level blocking.
- `paused` / `suspended`: the process is not selected for normal execution.
- `exited`: completed successfully.
- `failed`: completed with failure.
- `killed`: terminated by signal or runtime decision.

Terminal statuses are `exited`, `failed`, and `killed`.

## Images And Tool Tables

An `AgentImage` defines the default process prompt, tool table, default Skills,
context policy, safety profile, declared required capabilities, and optional
boot metadata. Fresh images boot from their manifest. Checkpoint-commit images
boot from an immutable internal runtime artifact derived from one checkpoint
root process.

Root process spawn may use image `required_capabilities` as a bootstrap
declaration for ordinary fresh images. `exec_process` and checkpoint-commit
image boot never grant those declarations automatically.

At process creation time, the runtime resolves image default tools into the
process tool table. A process can call only tools in that table, but visible
tools still fail at primitive use if resource authority is missing.

A checkpoint-commit image remaps baked Object Memory, process-local JIT tools,
loaded Skill records, and cwd into the new process. It does not package or
restore filesystem, shell, JSON-RPC, human, network, or provider side effects.

## Working Directory

Each process has its own workspace-relative working directory. Relative
filesystem paths and shell subprocess cwd resolve from that process cwd. The
runtime host process does not `chdir` into launched workspaces.

The CLI command:

```bash
uv run agent-libos --db .agent_libos.sqlite cd <pid> src
```

updates one process working directory and leaves other processes unchanged.

## Scheduler

The high-level async entrypoint is:

```python
results = await runtime.arun_until_idle(max_quanta=10)
```

By default it:

1. runs runnable processes,
2. processes pending human terminal messages when work is blocked on human I/O,
3. delivers process-message notices at tool boundaries,
4. wakes resumed processes,
5. stops when no runnable or human-resumable work remains, or when the quantum
   budget is exhausted.

Single-step APIs remain available for tests and debugging:

```python
result = await runtime.arun_next_process_once()
result = await runtime.arun_process_once(pid)
```

For debugging pending approval states, disable human queue processing:

```python
results = await runtime.arun_until_idle(max_quanta=1, process_human_queue=False)
```

## Human Queue

Human interaction is modeled as runtime objects, not raw prompt text.

- `ask_human` creates a blocking question.
- `request_permission` creates a policy or approval request.
- `human_output` writes through the HumanObject primitive and provider.
- Per-use approvals can create one-shot capabilities that are consumed after
  one successful primitive call.

If a primitive blocks on human approval, the process enters `waiting_human`.
The runtime can process human terminal messages, update the request, wake the
process, and resume the original operation. Rejection returns a normal failure
to the process instead of crashing the runtime.

## Process Messages And IPC

Each process has a durable message queue. Messages include:

- sender and recipient pid,
- `kind` such as `normal` or `interrupt`,
- channel,
- correlation id,
- reply target,
- subject and body,
- structured payload,
- delivery and acknowledgement state.

Processes can send messages to themselves, their parent, or direct children.
Receivers use `read_process_messages` for non-blocking reads or
`receive_process_messages` to block until matching unread messages arrive.
Filters can match kind, sender, channel, correlation id, reply target, and exact
message ids.

Interrupt messages preempt before non-message tool calls until read. Normal
messages notify after a tool call and do not block the current action.

CLI examples:

```bash
uv run agent-libos --db .agent_libos.sqlite message <pid> "Please inspect the result"
uv run agent-libos --db .agent_libos.sqlite interrupt <pid> "Stop and read this first"
```

## Fork, Spawn, Exec, Wait, Signal

`fork_child_process` creates a direct child with an attenuated parent
`MemoryView`. It can inherit selected capabilities only if the parent already
holds them.

`spawn_child_process` creates a fresh direct child with a new process namespace
and goal-only memory. It does not inherit parent-activated Skills or broad
external authority by default.

`exec_process` replaces the current image and tool table without changing pid.
It never grants the target image's declared required capabilities
automatically. Existing external capabilities are preserved only when explicitly
requested; otherwise exec shrinks external authority.

`wait_child_process` blocks the parent in `waiting_event`. Child exit wakes the
parent and resumes the original wait action without asking the model for a new
action.

Signals can pause, resume, cancel, interrupt, or terminate direct children.

## Process Exit

`process_exit` marks a process as `exited` or `failed` and can attach a final
Object Memory result. Process-owned memory is released on exit unless retained
as the process result.

When a Deno JIT tool calls `process.exit` or `process.exec`, the syscall records
a deferred lifecycle change. The runtime applies that change only after the JIT
tool returns its normal tool result.
