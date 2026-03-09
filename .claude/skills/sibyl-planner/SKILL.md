---
name: sibyl-planner
description: Sibyl 实验规划 agent - 设计严谨可复现的实验方案
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, WebSearch, WebFetch
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('planner'))"`

Mode: $ARGUMENTS[0] (`plan` | `fix-gpu`)
Workspace path: $ARGUMENTS[1]
Planning detail: $ARGUMENTS[2] (only for `plan` mode; e.g. pilot config summary)
