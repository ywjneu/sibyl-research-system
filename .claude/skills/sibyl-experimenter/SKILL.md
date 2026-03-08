---
name: sibyl-experimenter
description: Sibyl 实验执行 agent - 编写代码并在远程 GPU 上执行实验
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, WebSearch, WebFetch, mcp__ssh-mcp-server__execute-command, mcp__ssh-mcp-server__upload, mcp__ssh-mcp-server__download, mcp__ssh-mcp-server__list-servers
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('experimenter'))"`

MODE: $ARGUMENTS[0]
Workspace path: $ARGUMENTS[1]
SSH server: $ARGUMENTS[2]
Remote base: $ARGUMENTS[3]
GPU IDs: $ARGUMENTS[4]
Optional --tasks: $ARGUMENTS[5] (if present, format: --tasks=task_1a,task_1b)
