---
name: sibyl-strategist
description: Sibyl 战略顾问 agent - 从战略角度分析结果并建议下一步
context: fork
agent: sibyl-light
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('strategist'))"`

Workspace path: $ARGUMENTS
