---
name: sibyl-section-critic
description: Sibyl 章节评审 agent - 评审论文特定章节
context: fork
agent: sibyl-light
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('section_critic'))"`

Section: $ARGUMENTS[0]
Section ID: $ARGUMENTS[1]
Workspace path: $ARGUMENTS[2]
Read: $ARGUMENTS[2]/writing/sections/$ARGUMENTS[1].md
Write critique to: $ARGUMENTS[2]/writing/critique/$ARGUMENTS[1]_critique.md
