---
name: sibyl-server-experimenter
description: Sibyl 服务器端实验 agent - 在远程服务器上使用 Codex/Claude 本地执行实验
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, mcp__ssh-mcp-server__execute-command, mcp__ssh-mcp-server__upload, mcp__ssh-mcp-server__download, mcp__ssh-mcp-server__list-servers
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('server_experimenter'))"`

MODE: $ARGUMENTS[0]
Workspace path: $ARGUMENTS[1]
SSH server: $ARGUMENTS[2]
Remote base: $ARGUMENTS[3]
GPU IDs: $ARGUMENTS[4]
Experiment mode: $ARGUMENTS[5]
Server Codex path: $ARGUMENTS[6]
Server Claude path: $ARGUMENTS[7]
Optional --tasks: $ARGUMENTS[8] (if present, format: --tasks=task_1a,task_1b)
