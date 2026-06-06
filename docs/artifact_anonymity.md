# Artifact Anonymity Checklist

This checklist is the M0 artifact hygiene baseline for an anonymous systems
submission. It is not a legal review or a release checklist; it is the minimum
set of checks required before sharing a paper artifact or benchmark bundle with
reviewers.

## Anonymous System Name

Use `Primitive Agent Runtime` (`PAR`) as the temporary anonymous name in paper
drafts and anonymous artifact documentation. Do not rename the Python package,
repository title, public README heading, or runtime classes for M0. The alias is
only a double-blind writing convention.

## License Consistency

- `LICENSE` must contain Apache License 2.0.
- `pyproject.toml` must use `license = { text = "Apache-2.0" }`.
- README and artifact docs must not claim MIT, proprietary, or dual licensing.
- Generated distributions should include `LICENSE` and should not add a
  conflicting classifier.

## Double-Blind Content Scan

Before making an anonymous artifact branch or archive, scan all tracked files
and generated paper/artifact files for:

- author names, lab names, school names, school emails, and personal emails,
- personal GitHub, GitLab, homepage, cloud bucket, or institutional URLs,
- absolute local paths such as `C:\Users\...`, `/Users/...`, or `/home/...`,
- private API endpoints, dashboard URLs, project ids, tenant ids, and account ids,
- `.env` contents, API keys, access tokens, SSH keys, cookies, and credentials,
- LLM provider account metadata in logs, traces, screenshots, notebooks, or
  benchmark results,
- non-anonymous git remotes, branch names, tags, commit messages, and issue links,
- PDF, DOCX, PPTX, image, and archive metadata that may contain author identity.

Reviewers must not need real credentials to run the deterministic artifact
subset. Real-model experiments may be optional, but their instructions must
make the credential requirement explicit and must not embed secrets.

## Runtime Artifact Commands

These commands are the M0 baseline checks for the code artifact:

```bash
uv sync --frozen
uv run python -m compileall agent_libos tests scripts experiments benchmarks
uv run python -m unittest discover -s tests -v
```

For an anonymous artifact branch, add a fresh-clone dry run before submission:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

The Deno executable is optional for the Python unit suite. Tests that require a
real Deno installation should skip with a clear message when `deno` is missing.

## Documentation Consistency

- README is the current project entrypoint and documentation index.
- `agent_libos_design_doc.md` is a historical design archive.
- `docs/invariants.md` is the invariant-to-test map.
- `docs/architecture.md`, `docs/runtime_model.md`, `docs/capabilities.md`,
  `docs/object_memory.md`, `docs/tools_and_jit.md`, `docs/skills.md`,
  `docs/checkpoints.md`, `docs/cli.md`, `docs/development.md`, and
  `docs/benchmark.md` are the current implementation guides.
- `benchmarks/runtime_safety/schema.md` defines benchmark task shape for the M1
  runtime-safety harness.
- Documentation must not describe Python JIT, direct external framework
  adapters, real GitHub/MCP providers, or unsupported rollback semantics as
  current behavior.

## Submission Exit Gate

M0 is complete when:

- the license metadata is internally consistent,
- the CI workflow runs compile and unit tests on supported Python versions,
- every core invariant has test coverage or an explicit gap,
- benchmark task schema v0 exists,
- benchmark harness documentation exists,
- a one-page paper thesis exists,
- this anonymity checklist exists and is linked from README.
