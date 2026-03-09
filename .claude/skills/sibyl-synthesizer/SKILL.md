---
name: sibyl-synthesizer
description: Sibyl 综合决策者 agent - 综合多方观点生成最终研究提案
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, WebSearch
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('synthesizer'))"`

Workspace path: $ARGUMENTS
