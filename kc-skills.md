# kc-skills (Colon Skills)

kc-skills are kitty-claude specific skills, distinct from Claude Code's built-in skills.

## Invocation

- `::skill-name` - invoke a kc-skill (note the double colon)
- `::skill-name prompt` - invoke with additional prompt text

## Location

Skills are stored in `~/.config/kitty-claude/kc-skills/<name>.md`

## Commands

- `::skills` - list all kc-skills
- `::skill <name>` - create/edit a kc-skill (opens vim popup)
- `:skills-mcp` - enable the skills MCP server, then `:reload`

## Skills MCP Server

After `:skills-mcp` and `:reload`, Claude has access to:

- `create_skill` - create a new kc-skill
- `update_skill` - update existing skill content
- `read_skill` - read skill content
- `list_skills` - list all kc-skills

## Format

kc-skills are plain markdown files. The content is injected into the prompt when invoked.

## vs Claude Code Skills

| Feature | kc-skills (`::`) | Claude Code skills (`/`) |
|---------|------------------|--------------------------|
| Location | `~/.config/kitty-claude/kc-skills/` | `~/.claude/skills/` or `.claude/skills/` |
| Invocation | `::name` | `/name` |
| Scope | kitty-claude only | Any Claude Code session |
| Create | `::skill name` or skills MCP | `:skill name` |
