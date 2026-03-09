---
name: sibyl-reflection
description: Sibyl 反思 agent - 分析迭代产出，分类问题，生成改进计划
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('reflection'))"`

Workspace path: $ARGUMENTS[0]
Current iteration: $ARGUMENTS[1]
