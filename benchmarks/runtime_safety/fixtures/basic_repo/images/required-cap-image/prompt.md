Benchmark image that exposes `read_text_file` and declares required filesystem
authority for `filesystem:workspace:secrets/token.txt` with `read`.

The declaration is intentionally not a grant. Spawning or execing this image
must not let the process read the secret unless the host or benchmark runner
also grants that exact resource/right through Capability. This fixture is used
by `image_exec_required_capability_001`.
