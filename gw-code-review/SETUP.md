# Setup: gw-code-review MCP Server

This skill is installed once at user level (`~/.claude/skills/gw-code-review`)
and reused across every repo you review — it is not tied to any single
project's checkout, so it stays put when you switch branches or repos.

## 1. Install Dependencies

From this directory:

```bash
cd ~/.claude/skills/gw-code-review && uv sync
```

This creates `.venv/` and installs `mcp[cli]` (v2.x), `pydantic` and `pyyaml`
into it. The project itself is not a buildable distribution — `mcp_server.py`
is a flat script that adds its own directory to `sys.path` and imports the
local `tools/` package — so `pyproject.toml` sets `[tool.uv] package = false`
and declares no build backend. Don't `pip install -e .` it; there is nothing
to build.

## 2. Smoke Test (Optional)

Verify the tools work without any MCP client:

```bash
cd /path/to/your/repo && ~/.claude/skills/gw-code-review/.venv/bin/python ~/.claude/skills/gw-code-review/mcp_server.py --self-test --base master
```

Use the interpreter from `.venv` rather than a bare `python3` — the
dependencies are installed only there.

(`--root` defaults to the current directory, same as running `git` itself —
pass `--root /path/to/your/repo` explicitly if you'd rather not `cd` first.)

This prints:

- Detected tech stack (Django/FastAPI/React/etc)
- Changed files vs base branch
- Symbol-level changes and coherence checks
- Contract changes (DRF/FastAPI/Celery)
- Bounded query results for change, symbol, source, sibling, grep, and anchor lookups
- A short, root-relative runtime probe result
- Explicit reasons for representative empty query results

If all outputs appear without errors, the tools are ready.

## 3. Register with MCP Client

For Claude Code, register once at user scope so the server is available in
every repo you review:

```bash
claude mcp add gw-code-review --scope user -- ~/.claude/skills/gw-code-review/.venv/bin/python ~/.claude/skills/gw-code-review/mcp_server.py
```

Verify with `claude mcp list` — it should report `✔ Connected`.

Equivalent hand-written config (e.g. for `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "gw-code-review": {
      "command": "/Users/<you>/.claude/skills/gw-code-review/.venv/bin/python",
      "args": ["/Users/<you>/.claude/skills/gw-code-review/mcp_server.py"]
    }
  }
}
```

**Important:** Use absolute paths, and point `command` at the `.venv`
interpreter, not a system `python3`. `GW_REVIEW_ROOT` is optional —
if unset, the server operates on whatever directory it was launched from
(its `cwd`), same as `git` does. Most MCP clients launch server subprocesses
with the client's own working directory, so this "just works" when you have
the target repo open. Set `GW_REVIEW_ROOT` explicitly in the `env` block
above only if your client launches from somewhere unpredictable (e.g. always
`$HOME`) and you want this server pinned to one specific repo regardless.

## 4. Portability

This skill works with any Django/DRF/FastAPI/React/Celery/Pulumi project. Just point `GW_REVIEW_ROOT` at the target project's checkout. Not supported: Go, Rails, Terraform, Kubernetes.

## 5. Usage

Once registered, see [SKILL.md](./SKILL.md) for how to invoke a review.

### Typed lookup contract

Restricted lanes use schema version `review-lookup.v1`. Every lookup is scoped
to the prepared `review_id` and opaque `lane_id`, includes a concrete
`question`, and shares that lane's quota. The registered typed tools are
`get_change`, `find_symbol`, `get_source`, `find_siblings`, `grep_repo`,
`verify_anchor`, `changed_file`, `get_evidence_item`, and `match_pathspec`. `review_lookup` remains a compatibility
wrapper only. A dispatch payload carries the schema version; do not launch a
payload whose version differs from this contract.

`get_source(file=...)` reads a bounded whole file; selectors are optional and
mutually exclusive. `changed_file` reports membership only, while
`get_evidence_item` retrieves a bundle item by ID. Use `match_pathspec` for a
pinned Git pathspec fact before reporting command-semantics behavior.

Verifier verdicts are provenance-bound: candidate-scoped typed calls return
opaque receipt IDs, and `register_verifier_result` accepts only those receipts.
Alternatively, `verify_candidate_source_facts` issues a deterministic
pinned-source result. `consolidate_review_results` rejects free-form verifier
results, so an orchestrator cannot upgrade a verdict by rewriting evidence.

## 6. Troubleshooting

**`Unable to determine which files to ship inside the wheel` / `hatchling.build.build_editable` failed**
`uv` is trying to build this project as a package. Ensure `pyproject.toml`
still has `[tool.uv] package = false` and no `[build-system]` table.

**`No module named 'mcp.server.fastmcp'`**
An `mcp` 1.x-era import against an installed 2.x SDK. The server uses the 2.x
name (`from mcp.server.mcpserver import MCPServer`), and `pyproject.toml` pins
`mcp[cli]>=2,<3`. Re-run `uv sync` if the lock predates that pin.
