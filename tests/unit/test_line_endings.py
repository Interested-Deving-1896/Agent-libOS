from __future__ import annotations

import subprocess
from pathlib import Path


def test_tracked_text_files_use_lf_line_endings() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ['git', 'ls-files', '--eol'],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    offenders: list[str] = []
    for line in result.stdout.splitlines():
        metadata, _, path = line.partition('\t')
        if not path:
            continue
        fields = metadata.split()
        if len(fields) < 2:
            continue
        index_eol, worktree_eol = fields[0], fields[1]
        if index_eol in {'i/crlf', 'i/mixed'} or worktree_eol in {'w/crlf', 'w/mixed'}:
            offenders.append(f'{index_eol} {worktree_eol} {path}')

    assert not offenders, 'Tracked text files must use LF line endings:\n' + '\n'.join(
        offenders
    )
