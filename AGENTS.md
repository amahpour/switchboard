# Notes for coding agents working on switchboard

The house rules for every coding agent, whatever runs it, are in [CLAUDE.md](CLAUDE.md). Read it before changing anything. Nothing in it is specific to one harness.

The step-by-step procedures it names are skills in [`.agents/skills/`](.agents/skills/) (the same directory as `.claude/skills/`). If your harness doesn't load skills by itself, open the one you need and follow it: `.agents/skills/<name>/SKILL.md`. A skill written as `/name` there means "follow that skill".
