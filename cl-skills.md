# cl-skills (Colon Skills)

cl-skills are clauthing specific skills, distinct from Claude Code's built-in skills.

## Invocation

- `::skill-name` - invoke a cl-skill (note the double colon)
- `::skill-name prompt` - invoke with additional prompt text

## Location

Skills are stored in `~/.config/clauthing/cl-skills/<name>.md`

## Commands

- `::skills` - list all cl-skills
- `::skill <name>` - create/edit a cl-skill (opens vim popup)
- `:skills-mcp` - enable the skills MCP server, then `:reload`

## Skills MCP Server

After `:skills-mcp` and `:reload`, Claude has access to:

- `create_skill` - create a new cl-skill
- `update_skill` - update existing skill content
- `read_skill` - read skill content
- `list_skills` - list all cl-skills

## Format

cl-skills are plain markdown files. The content is injected into the prompt when invoked.

## vs Claude Code Skills

| Feature | cl-skills (`::`) | Claude Code skills (`/`) |
|---------|------------------|--------------------------|
| Location | `~/.config/clauthing/cl-skills/` | `~/.claude/skills/` or `.claude/skills/` |
| Invocation | `::name` | `/name` |
| Scope | clauthing only | Any Claude Code session |
| Create | `::skill name` or skills MCP | `:skill name` |
