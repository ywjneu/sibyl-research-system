---
name: sibyl-latex-writer
description: Sibyl LaTeX 排版 agent - 将论文转为 NeurIPS LaTeX 格式并编译
context: fork
agent: sibyl-standard
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash, mcp__ssh-mcp-server__execute-command, mcp__ssh-mcp-server__upload, mcp__ssh-mcp-server__download, mcp__ssh-mcp-server__list-servers
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('latex_writer'))"`

Workspace path: $ARGUMENTS[0]
SSH server: $ARGUMENTS[1]
Remote base: $ARGUMENTS[2]
