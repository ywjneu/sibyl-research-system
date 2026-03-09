---
name: sibyl-supervisor
description: Sibyl 监督审查 agent - 独立第三方质量审查
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, WebSearch
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('supervisor'))"`

Workspace path: $ARGUMENTS
