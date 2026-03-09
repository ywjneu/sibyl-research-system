---
name: sibyl-final-critic
description: Sibyl 终审 agent - NeurIPS/ICML 级别的论文终审
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('final_critic'))"`

Workspace path: $ARGUMENTS
