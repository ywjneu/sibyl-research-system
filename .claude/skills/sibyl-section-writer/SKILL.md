---
name: sibyl-section-writer
description: Sibyl 章节撰写 agent - 撰写论文特定章节
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('section_writer'))"`

Section: $ARGUMENTS[0]
Section ID: $ARGUMENTS[1]
Workspace path: $ARGUMENTS[2]
Write to: $ARGUMENTS[2]/writing/sections/$ARGUMENTS[1].md
