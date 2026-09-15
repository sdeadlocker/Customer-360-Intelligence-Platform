# Prompt registry

Versioned prompt templates, loaded at runtime from `PROMPT_REGISTRY_PATH` (default `prompts/`) by
`c360.agents.prompts.build_prompt_registry`.

Prompts are files rather than string literals in Python so that a prompt change is reviewable as a
diff and an evaluation run can be pinned to an exact prompt version (design §14.7). One active prompt
file per agent, named `<agent>.md`. This `README.md` is documentation and is ignored by the loader.

## File format

Each prompt is a UTF-8 `.md` file with a `---`-delimited front-matter header followed by the prompt
body:

```
---
id: financial_health
version: v1
owner: platform
agent: financial_health
field_allowlist: net_worth_cents, total_deposits_cents
knowledge_domains: procedure
---
You are the financial-health agent. ...
```

Header keys:

- `id`, `version`, `agent` — required. `agent` binds the prompt to a graph node.
- `owner` — optional, defaults to `unknown`.
- `field_allowlist` — comma-separated fields the `PromptRedactor` keeps for this agent (task 8.4).
- `knowledge_domains` — comma-separated retrieval domains; **empty means the agent does not retrieve**.

The loader computes a SHA-256 over the whole file and folds its first twelve hex chars into the
effective version (`v1+<hash12>`), so editing a body without bumping the header still produces a new
`prompt_version` on `AgentResult` and in the agent cache key (design §8.7).
