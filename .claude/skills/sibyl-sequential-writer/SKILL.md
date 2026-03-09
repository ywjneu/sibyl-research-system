---
name: sibyl-sequential-writer
description: Sibyl 顺序写作 agent - 按章节顺序写作，确保整体行文一致性
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('sequential_writer'))"`

Workspace path: $ARGUMENTS
