---
description: "迁移本地旧项目到 v5 结构"
argument-hint: "<project>"
---

# /sibyl-research:migrate

迁移本地旧项目到 v5 结构。

**所有用户可见的输出遵循项目语言配置（`action.language` / `config.language`）；论文正文与 LaTeX 始终使用英文。默认配置为中文。**

参数: `$ARGUMENTS`（项目名称）

执行以下命令：
```bash
cd $SIBYL_ROOT && .venv/bin/python3 -c "from sibyl.orchestrate import cli_migrate; cli_migrate('workspaces/$ARGUMENTS')"
```
