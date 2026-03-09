---
name: sibyl-skeptic
description: Sibyl 怀疑论者 agent - 以最大怀疑态度审视实验结果
context: fork
agent: sibyl-light
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('skeptic'))"`

Workspace path: $ARGUMENTS
