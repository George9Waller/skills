# gw-code-review-v2

`gw-code-review-v2` is a standalone candidate skill for reviewing Git changes.
It aims to find important defects and concrete maintainability improvements,
with review depth matched to the change's risk. It is intentionally separate
from `gw-code-review`; do not replace the original skill while evaluating this
candidate.

The candidate emphasizes pinned source evidence, tracing relevant callers,
careful wording for unresolved questions, behavior comparisons across risky
refactor boundaries, and filtering out comments that do not help the author.
See [SKILL.md](./SKILL.md) for the full reviewer instructions.

## Capture a review snapshot

The helper requires Python 3.10+ and Git. Save snapshots outside the repository
being reviewed. For a committed change:

```sh
python3 scripts/review_snapshot.py capture \
  --repo /path/to/repository \
  --base main \
  --head feature-branch \
  --output /tmp/review-snapshot
```

To capture staged, unstaged, and untracked changes against a base revision,
omit `--head` and use `--worktree`:

```sh
python3 scripts/review_snapshot.py capture \
  --repo /path/to/repository \
  --base main \
  --worktree \
  --output /tmp/review-snapshot
```

The output contains a manifest with immutable commit IDs, changed-path
inventory, hashes, and a complete diff, plus captured changed-file content.
The helper refuses incomplete or unstable captures and refuses to overwrite
an existing output directory.

## Inspect captured source

Use the `source` command to read a changed path from the pinned base or head:

```sh
python3 scripts/review_snapshot.py source \
  --snapshot /tmp/review-snapshot \
  --side head \
  path/to/file.py
```

Use `check-anchor` to verify a proposed comment location against captured head
source and the added lines in the diff:

```sh
python3 scripts/review_snapshot.py check-anchor \
  --snapshot /tmp/review-snapshot \
  --path path/to/file.py \
  --start 42 \
  --end 44
```

This helper captures and inspects review inputs; it does not invoke a model or
post comments to a pull request. Treat evaluation results as development
evidence, not proof of quality or affordability. Keep answer keys and prior
evaluation history out of reviewer contexts.
