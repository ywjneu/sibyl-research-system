---
name: sibyl-optimist
description: Sibyl 乐观分析者 agent - 从积极角度分析实验结果
context: fork
agent: sibyl-light
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('optimist'))"`

Workspace path: $ARGUMENTS
