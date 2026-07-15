# Task Authority Manifests

`TaskAuthorityManifest` is the Host-authored launch contract for an
`AgentProcess`. It closes the gap between an image declaring what it may need
and a real user, workflow controller, or enterprise policy deciding what this
particular task may receive.

The default `runtime.launch_authority_mode` is `manifest_required`. Image
`required_capabilities` are copied into the durable manifest as requirements
for comparison and Explain output, but they are never granted. Only
`authorized_capabilities` compile into root capabilities. The explicit
compatibility mode `legacy_image_grants` retains the older image-auto-grant
behavior and records the mode in the manifest and audit log.

A manifest records:

- the process, image, goal reference, issuer, parent manifest, expiry, and a
  deterministic SHA-256 hash;
- authorized and image-required capability specs;
- permitted provider effect classes;
- resource budget, approval policy, and process receive-domain data-flow policy;
- operator metadata that does not contain task payloads or credentials.

Root launch compiles only the authorized capability specs. Child launch uses
the intersection of the parent manifest, current capability policy, and the
child manifest ceiling. `CapabilityManager.derive_authority()` and
`transition_allowed_rights()` are the public transition boundaries used by
process and checkpoint flows; finite-use authority is never duplicated.
Manifest `max_delegation_depth` values are compiled into the durable
capability and may only stay equal or decrease across a transition.

Checkpoint fork creates a new hashed manifest for every remapped process in
the same transaction as its process and capability rows. Explicit source
manifests retain their effect, approval, expiry, budget, and data-flow
ceilings. An implicit source manifest remains an empty model-request contract,
so copied Host authority does not silently become requestable authority.

Every launch receives a durable manifest record. When the Host does not supply
an explicit template, that implicit record is still an empty model-request
contract: arbitrary permission requests are denied. It is not, however, a
transition ceiling for capabilities the Host deliberately grants after launch.
Only an explicit Host manifest (and manifests derived beneath it) constrains
later child/fork authority and manifest budgets. Derived manifests inherit the
parent approval, effect, expiry, and data-flow ceilings unless the Host supplies
a narrower child template. Omitted child fields inherit those ceilings. An
explicit child template cannot add or replace parent data-flow or approval
policy keys,
request authority outside the parent's authorized/requestable space, extend the
parent expiry, or turn a non-empty effect ceiling into the compatibility-mode
empty ceiling.

Model permission requests are checked against the manifest before a Human
request is created. `approval_policy.requestable_capabilities` declares
authority that may be requested but is not granted at launch; a Human decision
still creates the narrower one-time or policy capability. An explicit
`permitted_effects` list is an additional
provider-boundary ceiling with exact entries or terminal wildcards such as
`jsonrpc.*`. An empty list preserves capability-only effect gating for
compatibility; it does not grant a capability.

Per-use external-operation approval preallocates the eventual external
`effect_id` and binds it with the canonical argument hash and target state
version in the one-shot capability. The capability reservation carries that
same id into the provider intent, so a same-argument call cannot create a
different approved effect ledger entry.

CLI example:

```bash
uv run agent-libos spawn \
  --goal "review one report" \
  --authority-manifest-json '{
    "authorized_capabilities": [
      {"resource":"filesystem:workspace:reports/*","rights":["read"]}
    ],
    "permitted_effects": ["filesystem.read_text"],
    "resource_budget": {"max_tool_calls": 20},
    "metadata": {"policy":"review-v1"}
  }'
```

`POST /api/processes` and `POST /api/workflows/run` accept the same object as
`authority_manifest`. Explain summaries show the manifest id/hash, declared
authority, unmet image requirements, effect ceiling, budget, and policies.

The manifest is not a substitute for primitive capability checks. Tool
projection, Skills, image metadata, and requirement declarations remain
visibility or planning inputs only.

## Data identity domain

`data_flow_policy` has one strict v1 shape:

```json
{
  "schema_version": 1,
  "allowed_tenants": ["tenant-a"],
  "allowed_principals": ["analyst-a"]
}
```

Unknown keys, unsupported versions, wildcard/`mixed` entries, malformed
identities, and non-list fields fail closed. A child/fork manifest inherits the
parent sets when omitted and may only keep or remove entries. Empty sets allow
only data without a tenant/principal.

This policy controls what identity domain a process may receive through a
goal, message, result, Object Task notification, memory merge, fork, or exec.
Those boundaries preserve the source labels; observing a labeled message also
creates a metadata-only process carrier so later text goals or replies inherit
the same label.

It is deliberately not an external Sink allowlist or trust root. A manifest
cannot mark `llm:*`, `jsonrpc:*`, a file, executable, or Human recipient as
trusted, cannot lower a Host Sink clearance, and does not need to repeat a
trusted Sink pattern. External transmission is governed by the independent
Host registry described in [Data Flow](data_flow.md), followed by ordinary
capability, `permitted_effects`, approval, and budget checks.
