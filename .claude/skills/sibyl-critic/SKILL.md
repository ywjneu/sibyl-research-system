---
name: sibyl-critic
description: Sibyl 批评者 agent - 苛刻但公正的学术批评
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('critic'))"`

Workspace path: $ARGUMENTS
