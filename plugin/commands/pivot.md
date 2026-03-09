---
description: "强制 pivot 到备选研究 idea"
argument-hint: "<project>"
---

# /sibyl-research:pivot

强制将项目切换到备选 idea 方向。

**所有用户可见的输出遵循项目语言配置（`action.language` / `config.language`）；论文正文与 LaTeX 始终使用英文。默认配置为中文。**

工作目录: 项目根目录（通过 $SIBYL_ROOT 或 cd 到 clone 位置）

参数: `$ARGUMENTS`（项目名称）

按照 sibyl-research 编排循环中的 pivot 逻辑执行。
