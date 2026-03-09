---
name: sibyl-supervisor-decision
description: Sibyl 监督决策 agent - 分析实验结果决定 PIVOT 还是 PROCEED
context: fork
agent: sibyl-heavy
user-invocable: false
allowed-tools: Read, Write, Glob, Grep, Bash
---

!`.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt, load_common_prompt; print(load_common_prompt()); print('---'); print(load_prompt('supervisor_decision'))"`

Workspace path: $ARGUMENTS

SPECIAL TASK: Analyze experiment results and the debate opinions.
Read:
- $ARGUMENTS/exp/results/summary.md
- $ARGUMENTS/idea/result_debate/optimist.md
- $ARGUMENTS/idea/result_debate/skeptic.md
- $ARGUMENTS/idea/result_debate/strategist.md
- $ARGUMENTS/idea/proposal.md

Determine: PIVOT or PROCEED?
Write to $ARGUMENTS/supervisor/experiment_analysis.md
End with exactly: DECISION: PIVOT or DECISION: PROCEED
