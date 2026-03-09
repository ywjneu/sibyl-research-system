---
description: "查看所有西比拉研究项目状态"
---

# /sibyl-research:status

查看所有项目状态。

**所有用户可见的输出遵循项目语言配置（`action.language` / `config.language`）；论文正文与 LaTeX 始终使用英文。默认配置为中文。**

执行以下命令：
```bash
cd $SIBYL_ROOT && .venv/bin/python3 -c "from sibyl.orchestrate import cli_list_projects; cli_list_projects()"
```
