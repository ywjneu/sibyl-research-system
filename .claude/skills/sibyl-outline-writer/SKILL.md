---
name: sibyl-outline-writer
description: Sibyl 大纲撰写 agent - 生成论文详细大纲
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('outline_writer'))"`

Workspace path: $ARGUMENTS
