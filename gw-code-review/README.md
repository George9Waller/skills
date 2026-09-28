# gw-code-review

`gw-code-review` is an MCP-backed skill for reviewing a Git branch or pull
request diff. It prepares a pinned snapshot of the change, supplies compact
evidence to focused review lanes, and consolidates only supported findings into
an actionable review report.

## Goals and principles

- Emulate the quality of a thoughtful human review by selecting targeted checks
  for the kinds of changes actually present, rather than applying one generic
  checklist to every diff.
- Treat the checks as an extensible review framework: additional lenses can be
  introduced for particular languages, frameworks, architectures, or risk
  areas when deeper coverage is useful.
- Find concrete correctness, integration, documentation, and operational risks
  that a strong human reviewer would want to know about.
- Make claims traceable to a specific diff and source snapshot rather than a
  mutable working tree.
- Keep investigation bounded: review lanes receive targeted evidence and have
  controlled source lookups instead of unrestricted repository access.
- Prefer a smaller set of evidence-backed findings over speculative, style, or
  lint-only comments.
- State uncertainty explicitly when available evidence cannot support a
  conclusion.

## Installation

From the root of this repository, run the installer:

```bash
./install-gw-code-review.sh
```

It shallow-clones the repository to a temporary directory, then copies only
`gw-code-review` to `~/.claude/skills/gw-code-review`; it does not replace the
rest of your skills folder. If that skill is already installed, it asks before
replacing it, so the same command can be used for updates.

Then install the skill's Python dependencies:

```bash
cd ~/.claude/skills/gw-code-review
uv sync
```

If you copied this directory into your skills folder manually, start at the
`uv sync` step instead.

Register the MCP server once at user scope:

```bash
claude mcp add gw-code-review --scope user -- ~/.claude/skills/gw-code-review/.venv/bin/python ~/.claude/skills/gw-code-review/mcp_server.py
```

Confirm the registration with `claude mcp list`. The server works against the
repository open in your client, or against the directory named by
`GW_REVIEW_ROOT`. See [SETUP.md](./SETUP.md) for a smoke test, alternative MCP
configuration, troubleshooting, and supported technology details.
