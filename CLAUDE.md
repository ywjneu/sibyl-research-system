# Sibyl System

## Python 环境（强制规则）

本项目使用 **venv** 环境，位于 `.venv/`（Python 3.12，基于 conda base 创建）。

**所有 Python 调用必须使用 `.venv/bin/python3`**，禁止使用裸 `python3`。

原因：系统 `python3` 指向 homebrew Python 3.14，缺少 `pyyaml`、`rich` 等依赖，会导致 `import yaml` 等失败。

```bash
# 正确
.venv/bin/python3 -c "from sibyl.orchestrate import cli_next; cli_next('...')"
.venv/bin/pip install <package>

# 错误
python3 -c "from sibyl.orchestrate import ..."
pip install <package>
```

依赖声明在 `requirements.txt`。如需重建环境：
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 工作目录

所有 Sibyl CLI 命令（`cli_next`, `cli_record` 等）必须在项目根目录 `/Users/cwan0785/sibyl-system` 下执行，因为 `from sibyl.xxx` 依赖包路径。

## 模型选择建议
- 默认 session 模型: **Sonnet**（最佳性价比）
- Sibyl action 输出包含 `model_tier` 字段，按需切换
- 纯轻量任务（交叉批评、结果辩论）可切 Haiku 节省额度
