# Capabilities

Agent libOS uses capabilities as the runtime authority subsystem. A visible
tool, activated Skill, JIT tool, child-process handle, object name, path string,
image id, checkpoint id, or JSON-RPC endpoint id is not enough to perform a
protected operation. Protected effects are authorized at primitive use by
process identity, typed resource pattern, right, effect, constraints, human
approval, and audit. This lets self-evolving agents change their action surface
without implicitly changing what resources they can affect.

## Capability Record

Capability records are structured authority statements:

- `subject`: process or runtime actor that holds the authority.
- `resource`: canonical typed resource pattern.
- `rights`: operation rights such as `read`, `write`, or `execute`.
- `effect`: `allow`, `deny`, or `ask`.
- `issuer`: actor that issued the record.
- `issuer_cap_id` and `parent_cap_id`: lineage for grant/delegation decisions.
- `delegation_depth`: attenuation depth from a parent capability.
- `issued_at`, `expires_at`, `uses_remaining`, and `status`.
- `rules`, `lease`, `delegation`, `constraints`, and `metadata`.

One-shot authority is not encoded as a policy string. It is an `allow`
capability with `uses_remaining=1`; committed primitive use consumes it and
revokes the capability when the count reaches zero. If one-shot Object Memory
authority is resolved through a namespace/name lookup, any handle minted from
that lookup remains one-shot; name lookup cannot turn temporary authority into
a persistent object handle.
`require(...)` atomically consumes finite-use authority by default. For
multi-step effects, primitives opt out with `consume=False`, reserve the exact
use before creating human, Skill, ObjectTask, or provider side effects, and
then commit or restore that reservation token. An explicit revoke invalidates
outstanding tokens, so late cleanup cannot reactivate revoked authority.
Provider subsystems do not manage these tokens directly: the
[`Protected Operation SDK`](protected_operation_sdk.md) reserves every distinct
finite decision in the same transaction as local prepare state and the effect
intent, then commits on the first effectful provider phase. The public
`CapabilityManager.restore_reserved_use(...)` operation is revoke-safe; the SDK
is its sole provider-effect caller.
Reservations left in flight by a crashed runtime are abandoned fail-closed on
the next open. A reserved use is restored only when an operation fails before
the effect begins; after the commit/provider boundary, the one-shot use remains
consumed even if the remote or follow-on result is a failure.
Checkpoint inspect/diff/replay reserve the selected exact-checkpoint or
checkpoint-process read lease across the diagnostic, restore it if the
diagnostic raises, and commit it once on success. Actor-mode checkpoint list
uses the ordinary immediate `require(...)` path. Cross-actor ObjectTask
get/wait consumes one selected finite read lease; list consumes each distinct
finite read lease used by its returned rows at most once; cancel consumes the
selected finite write lease after terminal/unsafe-cancellation preflight.
Internal wait polling does not repeatedly consume authority.
Capability issuance itself commits the new row, process attachment, event,
audit, and issuer reservation as one transaction.

Launch authority is additionally bounded by a metadata-only
[`TaskAuthorityManifest`](task_authority_manifest.md). Image requirements do
not compile into capabilities. Model permission requests are rejected before a
Human prompt when the requested resource/right exceeds the manifest.

Human approval for a concrete external operation adds an
`approval_binding` constraint containing an effect id, canonical argument hash,
and optional target state version. A resumed operation with changed arguments
or a changed supplied target version cannot consume that one-time capability.

`deny` records dominate matching allows. To create an exception, revoke the
broad deny and issue narrower allow/deny records explicitly. The runtime does
not implement hidden override precedence that could accidentally reopen a
blocked resource, and primitive-specific candidate filtering must reapply the
same deny-first ordering before making a decision.

Authority derivation uses public CapabilityManager transition APIs.
`derive_authority()` applies source authority and an optional manifest ceiling;
`transition_allowed_rights()` reapplies expiry, finite-use duplication rules,
and current restrictive policy for checkpoint/fork/restore transitions. These
surfaces replace subsystem-local resource matching as the transition policy
boundary. A single delegation commits its capability row, process attachment,
grant event, and delegation audit together. Batch derivation validates every
requested spec before publishing the first child record, then commits all
delegations and the transition summary in one transaction. A late validation
or evidence-sink failure therefore publishes none of the batch.

## Rights

The common rights are:

- `read`: inspect or materialize a resource.
- `write`: create or modify a resource.
- `delete`: remove a resource.
- `execute`: run or load a resource.
- `materialize`: include object content in a prompt or tool result.
- `link`: create Object Memory links.
- `diff`: compare object or checkpoint state.
- `grant`: issue authority over a covered resource.
- `revoke`: revoke authority over a covered resource.
- `approve`: approve a human/request resource.
- `admin`: perform destructive or policy-changing operations.

The exact right set depends on the primitive. Unknown or unsupported rights do
not create primitive behavior by themselves. Capability records reject unknown
rights, including `*`; use explicit rights instead of all-rights wildcards.

## Resource Matching

Resources are typed and canonicalized before authorization. Matching is not a
raw `startswith` or suffix check.

Important resource conventions include:

- `filesystem:workspace:<path>` for exact workspace files and directories.
- `filesystem:workspace:<dir>/*` for a directory subtree.
- `filesystem:workspace:*` for the whole workspace namespace.
- `shell:<executable>` for direct command authority; `shell:*` for shell
  policy records when paired with `shell_policy_level`.
- `human:<name>` for human output, questions, and approvals.
- `clock:now`, `clock:sleep`, and `clock:*` for clock reads and bounded sleep.
- `object:<oid>` for Object Memory content.
- `object_namespace:<namespace>` for Object Memory namespace listing and name
  lookup.
- `process:<pid>` and `process:*` for process operations.
- `process:spawn` for child process and ObjectTask runner creation.
- `image:<image_id>` and `image:*` for image registration.
- `skill:<skill_id>` and `skill:*` for Skill operations.
- `skill_source:workspace:<relative_path>` and
  `skill_source:global:<source_id>` for Skill source authority.
- `skill_trust:*` or `skill_trust:<sha256>` for global Skill trust.
- `checkpoint:process:<pid>`, `checkpoint:<checkpoint_id>`, and
  `checkpoint:*` for checkpoint operations.
- `jsonrpc_endpoint:<endpoint_id>` and `jsonrpc_endpoint:*` for JSON-RPC
  endpoint registry metadata.
- `jsonrpc:<endpoint_id>:<method_id>`, `jsonrpc:<endpoint_id>:*`, and
  `jsonrpc:*` for JSON-RPC method invocation.
- `mcp_server:<server_id>` and `mcp_server:*` for MCP server registry
  metadata.
- `mcp:<server_id>:<tool_id>`, `mcp:<server_id>:*`, and `mcp:*` for MCP tool
  invocation.

Wildcard syntax is terminal only. `kind:*` is a typed prefix pattern and
`kind:body/*` is a subtree pattern. Bare global `*` is rejected; authority must
stay inside an explicit resource kind. Prefix collisions are rejected: a grant
for `filesystem:workspace:src/*` covers `src/main.py`, not `src2/main.py`.

Requested resources are produced by primitives after their own normalization.
For example, the filesystem primitive resolves cwd-relative paths, enforces
workspace containment, then asks for the canonical logical resource. Shell
authority uses normalized executable identity and argv token policy, not a shell
string.

## Authorization API

The manager entry point is:

```python
authorize(subject, resource, right, context) -> CapabilityDecision
```

`require(...)` wraps `authorize(...)`, raises on denial, and claims finite-use
authority before returning. Effect adapters that need pre-commit compensation
must call `require(..., consume=False)` and use
`reserve_decision_use`/`commit_reserved_use`; raw authorization decisions are
not reusable effect tickets. Primitive code should pass operation context such
as path, argv, byte counts, hashes, risk labels, process lineage, or provider
details. The decision records:

- matched capability ids,
- selected capability id,
- issuer chain,
- effect and derived human-facing policy,
- constraint evaluation results,
- one-shot consumption id when applicable,
- human approval request id when a primitive asks the human,
- operation context preview.

Unknown constraint keys fail closed. Primitive-specific evaluators can define
new constraint keys, but the default manager does not silently ignore unknown
policy language.

## Authority Rules And Profiles

Capabilities can carry deterministic `AuthorityRule` entries. A rule has:

- `operation`, such as `filesystem.read`, `shell.run`, `jsonrpc.call`,
  `mcp.call`, or `deno.syscall`;
- `effect`: `allow`, `ask`, or `deny`;
- `risk`: `harmless`, `low`, `medium`, `high`, or `destructive`;
- structured `conditions`, such as argv tokens, match mode, path/cwd intent,
  network intent, and filesystem intent.

Rules are not LLM judgments. They are local, deterministic policy facts. Unknown
rule shapes, unknown constraint keys, and malformed values for known conditions
fail closed. The primitive converts the final capability decision into a sandbox
profile and records that profile in approval context, audit, and external-effect
metadata where applicable. In particular, `timeout_s` and `timeout_max_s`
conditions, and the operation `timeout_s` compared against them, must be finite
non-negative numbers. Booleans, NaN, positive or negative infinity, and negative
values are malformed and fail closed; zero and fractional values are valid.

## Issue, Delegate, Revoke

All authority mutation goes through explicit operations:

- `issue(actor, subject, spec)`: `actor` must hold covering `admin` authority,
  or hold both covering `grant` authority and covering
  `allow` capabilities for every right being transferred. `grant` is not a
  capability-minting right: it can only transfer rights the actor already has,
  cannot create `deny`/`ask` policy records, and cannot transfer finite-use
  capabilities onward. Overlapping `deny` or `ask` boundaries, or malformed
  authority rules on the covering parent, fail closed before transfer.
- `delegate(parent, child, spec)`: `parent` must hold a covering delegable
  `allow` capability. Delegation can only attenuate resource, rights, expiry,
  constraints, and delegation depth. Finite-use capabilities are consumed by
  direct use and cannot be delegated. Delegated records keep a parent link, so a
  later parent revocation or expiry stops the child record from authorizing.
  Child records cannot drop parent constraints such as `shell_policy_level`.
  Delegation also cannot launder an overlapping parent `deny`/`ask` or malformed
  authority rule by selecting a narrower allow. The child record is not
  observable unless its process attachment, event, and audit evidence all
  commit.
- `revoke(actor, cap_id)`: allowed for the original issuer, the holder
  relinquishing its own capability, or an actor with covering `revoke`/`admin`
  authority. Target mutation, finite authority reservation/commit, the revoke
  event, and revoke audit all share one store transaction. A validation or
  evidence-sink failure therefore neither publishes the revocation nor consumes
  its finite authority.

Runtime bootstrap, image bootstrap, human approval, admin CLI, checkpoint
restore/fork, and tests use explicit embedding-host paths such as
`issue_trusted()` and emit audit records. These methods, along with operations
that explicitly set `require_authority=false`, are host API bypasses: the actor
string is attribution, not authentication, and no configured name or prefix
makes an ordinary caller trusted. They must not be exposed to an AgentProcess,
model, Skill, or JIT tool. Ordinary execution must use the checked `issue()`,
delegation, and revocation paths and has no implicit signing authority.

Fork and spawn inherit authority only through delegation/attenuation. Exec
switches the image/tool table and may shrink capabilities, but it never grants
the target image's declared `required_capabilities`.

Image-package boot is also not an external authority grant. Its
`workspace.grants` entries apply only to the package workspace seed after it is
materialized into that process's private directory under
`agent_outputs/image_workspaces/`; they cannot name arbitrary host or workspace
paths.

## Permission Policy And Human Approval

Stored capability policy names are:

- `always_allow` maps to `effect=allow`.
- `always_deny` maps to `effect=deny`.
- `ask_each_time` maps to `effect=ask`.
- `allow_once` maps to `effect=allow, uses_remaining=1`.

A terminal response to a `permission_request` must explicitly choose one of
`always_allow`, `always_deny`, or `ask_each_time`; it cannot choose
`allow_once`. One-shot authority is requested/issued through the separate
one-time capability lease shape so its exact use count remains explicit.
Approved responses cannot install `always_deny`, rejected responses cannot
install `always_allow`, and the JSON `approved` boolean must agree with the
terminal status. Approved ordinary questions require a non-empty string
`answer` rather than implicit coercion.

Model-facing `request_permission` is not a raw grant API. It first requires the
caller to hold `human:<name>` write authority, then creates a blocking human
request. Before a request enters the human queue, the runtime canonicalizes the
resource, normalizes rights, classifies risk, records resource scope, attaches
any deterministic constraints, and shows the selected lease shape. Ordinary
model requests cannot ask for broad high-risk authority such as
`capability:*` with privileged rights (`admin`, `grant`, `revoke`, `write`,
`execute`, or `delete`), `shell:*` execute, or root/global filesystem write such
as `filesystem:/:*` or `filesystem:*`. Workspace-level write
(`filesystem:workspace:*`) can be approved by the human. Admin CLI and
bootstrap paths can still issue broader policy explicitly, with audit.

When `ask_each_time` applies, the primitive creates a human approval request and
waits inside the operation. The caller eventually receives either the final
payload or a final denial error; there is no exposed pending/retry syscall
protocol.

Approval context includes path, resource, caller-declared overwrite policy,
byte count, SHA-256, argv, risk, rule id, sandbox profile, and escaped previews
when available. Filesystem target state is deliberately omitted until the
operation has received and reserved authority, so an approval prompt cannot be
used as an existence or metadata oracle.

`human_output` requires `human:<name>` write and reserves finite-use authority.
Before provider delivery, one transaction marks its request `delivered` and
persists the output event, audit record, and a structured pending external-effect
intent. Provider failure finalizes unknown evidence when possible; successful
delivery followed by classifier/finalization failure leaves the pending intent
and still returns without replay. The terminal queue cannot deliver that request
again, and the one-shot use is not restored after the provider boundary.

Terminal queue questions, permission-policy prompts, and ordinary approval
prompts also cross the configured Human provider through structured `read` or
`write` intents. Interactive answers and automatic decisions settle the same
pending effect id, but their audit/effect observations persist only request id,
purpose, lengths, byte counts, and SHA-256 values. Raw prompt text, raw answers,
and Human-provider exception text are never written to those records. If the
provider interaction succeeds but later event, audit, classification, or CAS
settlement fails, the request still commits its answer/policy so draining the
queue cannot show the prompt again; the unresolved intent remains pending.
Human output provider failures likewise persist only `provider_error_type`, not
the exception message.

## Tool, Skill, And JIT Boundary

The process tool table controls LLM-facing tool visibility. Capabilities
control primitive effects.

`ToolPolicy` is declaration metadata only. Fields such as
`declared_permissions` and `declared_confirmation_required` can help a GUI or a
human reviewer understand a tool, but the broker does not convert them into
grants or confirmations. Real authorization still happens in the primitive that
touches the resource.

For example, a process can see `write_text_file` and still fail to write
`src/app.py` if it lacks write authority for
`filesystem:workspace:src/app.py` or a covering subtree grant.

Loading a Skill can add instructions, existing tools, and Deno/TypeScript JIT
tools to one process table. It does not grant filesystem, shell, human, object,
process, image, checkpoint, JSON-RPC, MCP, or Skill source authority. JIT
syscalls bypass the LLM-facing tool table, but they still enter the same
primitive authorization path as built-in tools.

Default images expose `list_capabilities` and `inspect_capability` so a process
can understand its own authority. `delegate_capability` and `revoke_capability`
are registered static tools but are not included in default image tool tables.

## Process Messages

Process messages are IPC records owned by the runtime message manager, not a
separate `message:*` capability namespace. Current authorization is based on
process relationship and target identity: a process can receive its own
messages, parents and direct children can communicate through the exposed
message tools, and filters such as channel, correlation id, or explicit message
ids limit delivery. Message tool visibility still matters, but visibility does
not grant unrelated process or Object Memory authority.

## CLI And Syscalls

The CLI supports:

```bash
uv run agent-libos capabilities list [--subject <pid>] [--include-inactive]
uv run agent-libos capabilities inspect <capability_id>
uv run agent-libos capabilities grant <subject> <resource> --rights read write
uv run agent-libos capabilities delegate <parent> <child> <resource> --rights read
uv run agent-libos capabilities revoke <capability_id> [--reason "..."]
uv run agent-libos capabilities explain <subject> <resource> <right>
```

Without `--actor-pid`, CLI commands run as audited admin operations. With
`--actor-pid`, the command is executed as that process and strict capability
checks apply. `--actor-pid` is a `capabilities` command option and must appear
before the subcommand, for example:

```bash
uv run agent-libos capabilities --actor-pid <pid> list
```

Deno/TypeScript JIT tools can use the syscall names:

- `capability.list`
- `capability.inspect`
- `capability.request_permission`
- `capability.delegate`
- `capability.revoke`
- `mcp.list`, `mcp.inspect`, `mcp.tools`, and `mcp.call` for registered MCP
  servers and tools.

Syscalls do not consult the process tool table. They are authorized by pid,
capability records, primitive rules, human approval, and audit.

## JSON-RPC Authority

Remote JSON-RPC calls are pre-registered endpoint resources. Agents cannot pass
URLs or secrets at call time.

Registry inspection and mutation use endpoint resources:

```text
jsonrpc_endpoint:demo-weather
jsonrpc_endpoint:*
```

Method calls use method resources:

```text
jsonrpc:demo-weather:forecast
jsonrpc:demo-weather:*
jsonrpc:*
```

The required right comes from the endpoint method spec: `read`, `write`, or
`execute`. Granting `jsonrpc_endpoint:* read` allows endpoint discovery, not
method invocation. Granting `jsonrpc:demo-weather:forecast read` allows that
specific remote method, subject to primitive validation, human approval,
runtime DNS policy, provider classification, audit, and external-effect
recording. Agent-facing inspect paths do not expose endpoint URLs or header
prefix/suffix values; those are host registry details.

## MCP Authority

MCP servers are pre-registered host provider resources. Agents cannot pass MCP
server commands, URLs, environment variable names, credentials, or arbitrary
remote tool names at call time.

Registry inspection and mutation use server resources:

```text
mcp_server:demo-tools
mcp_server:*
```

Tool calls use tool resources:

```text
mcp:demo-tools:echo
mcp:demo-tools:*
mcp:*
```

The required right comes from the server manifest's allowlisted tool spec:
`read`, `write`, or `execute`. Granting `mcp_server:* read` allows server
discovery, not tool invocation. Granting `mcp:demo-tools:echo read` allows only
that manifest-declared tool, subject to argument schema validation, live tool
schema checks, runtime DNS/secret policy for HTTP transports, provider
classification, audit, and external-effect recording. MCP Resources and
Prompts are not exposed in v1.

For `stdio` MCP transports, actor-mode server registration, live tool refresh,
and tool calls additionally require `process:spawn` `write`, because those
operations can start a local child process. Host/admin paths that explicitly
bypass actor capability checks remain audited host operations.

## Filesystem Authority

Filesystem capabilities can target exact files, directory subtrees, or the
whole workspace. Relative paths resolve from the caller process working
directory. The filesystem primitive enforces workspace containment before host
provider calls. Path strings that escape the workspace are rejected even if a
model can produce them.

Read, write, and delete are separate rights. Granting read over a directory does
not grant write or delete.

Process cwd selection is a filesystem read, not ambient process metadata.
`set_working_directory`, explicit spawn/fork working directories, and explicit
PTY cwd values require `read` on the selected directory resource. Higher-level
spawn/image or shell authorization is checked before the cwd state probe, and
the probe uses the normal filesystem pending-intent and finite-use semantics.

Write/delete authority, including an `ask` decision, is resolved before the
primitive calls `state()`. An unauthorized or rejected mutation therefore
cannot probe whether a target exists, its kind, or its size. After approval, the
exact finite-use mutation right is reserved and one pending effect intent spans
both `state()` and the mutation. Read/list similarly use one reservation and
intent across their state and data/metadata reads.

That shared intent is an authorization and durable-evidence boundary, not a
serializable host-filesystem transaction. Runtime state checks and provider
mutation are separate host operations, and another host process can race them.
The provider therefore revalidates containment and no-follow conditions at the
mutation boundary, but callers must not interpret the prior `state()` result as
a globally locked filesystem snapshot.

If the first provider observation certifies `ProviderEffectNotStarted`, the
reservation and pending row are atomically restored/abandoned. If `state()`
already returned information but the main mutation then certifies not-started,
the mutation one-shot is restored while the same effect id is finalized as
`state_mutation=false, information_flow=true`. Ordinary state/read/mutation
exceptions cannot prove what was observed or changed and finalize or retain a
conservative unknown outcome.

The default local filesystem provider performs no-follow traversal inside the
workspace root for existing files. Existing file read, write, and delete reject
symlink or junction traversal and reject regular files with multiple hard links
(`st_nlink > 1`). Directory listings report child symlinks as symlink entries
without following them.

Bounded reads do not trust the size returned by an earlier `state()` call. If
that snapshot does not already prove the file is oversized, the primitive asks
the provider for one internal sentinel byte beyond `max_bytes`; a file that
grows between state and read is therefore returned at the caller's bound with
`truncated: true`, not mislabeled as complete. The sentinel is not exposed or
charged as information flow: `bytes_read` and `external_read_bytes` count only
the selected bytes up to `max_bytes`. If the original state already exceeds the
bound, the provider reads only the bound because truncation is already known.

Authorization is also separated from external-effect evidence. Filesystem,
clock, and shell primitives persist a pending `unknown` effect intent after
local preflight but before the first provider call. A classified result
conditionally finalizes that same `effect_id`; after information flow or
mutation may have begun, a post-provider capability/event/audit/classifier
failure cannot make the durable effect history look empty.

`clock.sleep` and async `clock.asleep` create that intent before the first
provider `monotonic()` measurement. The elapsed-time observations make the
whole composite operation `information_flow=true`. Only
`ProviderEffectNotStarted` from that first measurement can atomically restore a
reserved one-shot use and abandon the intent. An ordinary first-measurement
exception, or any sleep/cancellation/final-measurement exception after the
first observation (including a later `ProviderEffectNotStarted`), consumes the
use and finalizes a conservative `unknown` effect.

## Shell Authority

Shell execution is argv-only. The model-facing tool and syscall accept token
arrays, not shell command strings. Pipes, redirects, wildcard expansion, and
command chaining must be requested explicitly through an interpreter executable
such as `bash`, `sh`, `cmd`, or `powershell`, where policy matching can inspect
the interpreter token.

Arguments beginning with a `file:` URL are rejected before the provider runs.
For bare executables, the local provider resolves argv[0] on a safe host PATH
and refuses a resolution inside the workspace or selected process cwd. Shell
subprocesses receive a constrained environment with `HOME` and `USERPROFILE`
pointing at the workspace root instead of the host user's real home.

An authorized Shell subprocess still runs as the host user. Argv policy, safe
PATH resolution, the constrained environment, and resource monitoring are not
an operating-system filesystem or network sandbox. Filesystem and JSON-RPC
Capabilities govern their corresponding runtime primitives; they cannot mediate
direct file or network I/O performed by an authorized child executable. Use the
Deno JIT boundary when code requires an OS-backed syscall allowlist. On Windows,
the local Shell provider rejects budgeted execution that supplies
`SubprocessLimits` because it cannot enforce that profile; unbudgeted Shell
execution may still run, while Deno uses its separate Windows supervision and
budget backend.

Shell command risk is classified by argv-token rules before the provider runs:

- `harmless`: read-only status/version/inspection commands such as
  `git status --short` or `python --version`.
- `low`: read-only project inspection such as `git diff`.
- `medium`: project code execution such as `pytest`, pytest collection,
  `npm test`, or `uv run ...`.
- `high`: package managers, network-capable tools, script interpreters,
  `python -m compileall`, service startup, and other commands likely to change
  host state or cross a boundary.
- `destructive`: delete/move/permission/system-control operations. These are
  denied by the built-in rule set even under broad shell policy.

The built-in shell policy levels then decide how to handle the classified rule:

- `always_deny`: reject every shell command.
- `allowlist_auto_else_ask`: allow `allow` rules and ask for `ask` rules.
- `blocklist_ask_else_auto`: use the same deterministic risk rules but is
  intended for broader local operation.
- `always_allow`: allow non-destructive commands while still reporting risk.

Those four strings are fixed semantic labels, not remappable config fields.
Configuration can select `default_policy_level` and replace the argv rule lists,
but an overlay cannot redefine, for example, `always_allow` to mean a different
level. Legacy `*_level` remapping fields are rejected by strict config loading.

The capability record's own effect is evaluated before its policy level, so an
`ask` or `deny` shell-policy capability cannot be converted into an automatic
allow by setting `shell_policy_level=always_allow`. A finite-use command or
policy allow is reserved after validation and intent recording, immediately
before provider execution. The exact reservation is restored only when the
provider raises `ProviderEffectNotStarted`, certifying that execution never
began. Timeout, resource-limit, cancellation, ordinary provider failure, or a
post-effect classification failure commits the use and records a conservative
`unknown` external effect; a failed tool result is not proof that the command
did nothing.

Rules match tokenized argv, not arbitrary substrings. Bare executable names do
not match path-qualified executables by accident. The local provider executes
the argv vector directly with `shell=False`; the primitive never rebuilds a shell
command string from model input. As defense in depth, non-`always_allow`
automatic allow rules downgrade to human approval when argv tokens contain shell
metasyntax such as command substitution, backticks, separators, newlines,
redirection/process substitution, brace expansion, or comments.
Direct command capabilities such as `shell:git` or `shell:git:*` can allow a
specific normalized executable, but a bare direct command grant is intentionally
limited: without `authority_rules`, it only covers argv that the deterministic
classifier already marks `allow`. Medium/high shell side effects need explicit
rules, per-use human approval, or a human/admin-issued shell policy. A bare
`shell:* allow` capability is not treated as direct command authority;
whole-shell authority must be represented as a policy capability carrying
`shell_policy_level`, so the primitive can still apply the four-level shell
policy semantics. Broad `deny` and `ask` records remain conservative
constraints.

For the exact built-in inspection argv `git status`, `git status --short`,
`git branch --show-current`, `git rev-parse --show-toplevel`, `git diff`, and
`git diff --stat`, the shell primitive injects `--no-optional-locks`, disables
`core.fsmonitor`, and adds `--no-ext-diff` for the two diff forms.
Authorization and returned results retain the original argv. Configured or
human-approved Git argv outside this exact set do not receive this rewrite and
must be assessed under their own rule/approval context.

Scoped denies are supported with `AuthorityRule` constraints. An unconstrained
deny still dominates all matching grants; a constrained deny dominates only when
its rule matches the current operation context, so policy can allow read-only
`git` inspection while denying `git push`.

Block-list checks also scan nested executable-looking argv tokens such as
`bash`, `powershell`, `python`, or `curl`.
