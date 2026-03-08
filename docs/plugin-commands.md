# Plugin Commands Reference

All commands are prefixed with `/sibyl-research:` in Claude Code.

## Core Commands

### `/sibyl-research:init`

Interactive initialization. Generates a `spec.md` requirements file and creates the workspace.

```
/sibyl-research:init
```

### `/sibyl-research:start <project>`

Start the autonomous research loop. Enters continuous iteration via Ralph Loop.

```
/sibyl-research:start my-project
```

### `/sibyl-research:continue <project>`

Resume a project from its current stage. Re-enters the orchestration loop.

```
/sibyl-research:continue my-project
```

### `/sibyl-research:resume <project>`

Resume a **paused** project (paused by rate limits, errors, or manual `/stop`). Different from `continue` — this first calls `cli_resume()` to clear the paused state.

```
/sibyl-research:resume my-project
```

### `/sibyl-research:status`

View status of all research projects (stage, iteration, score, errors).

```
/sibyl-research:status
```

### `/sibyl-research:stop <project>`

Stop the research project and close the Ralph Loop.

```
/sibyl-research:stop my-project
```

## Research Control

### `/sibyl-research:debug <project>`

Single-step mode. Executes one pipeline stage at a time, waiting for confirmation before advancing. Useful for debugging and understanding the pipeline.

```
/sibyl-research:debug my-project
```

### `/sibyl-research:pivot <project>`

Force a PIVOT — abandon the current research direction and return to idea debate with alternative proposals.

```
/sibyl-research:pivot my-project
```

## Sync & Evolution

### `/sibyl-research:sync <project>`

Manually sync research data to Feishu/Lark cloud documents. Normally triggered automatically after each stage.

```
/sibyl-research:sync my-project
```

### `/sibyl-research:evolve`

Run cross-project evolution analysis. Extracts lessons from all completed projects and generates agent prompt improvements.

```
/sibyl-research:evolve
```

## Migration

### `/sibyl-research:migrate <project>`

Migrate a local project from an older workspace structure to the current version.

```
/sibyl-research:migrate old-project
```

### `/sibyl-research:migrate-server <project>`

Initialize or migrate the server-side directory structure for a project. Creates `projects/<project>/`, `shared/`, and `registry.json` on the remote server.

```
/sibyl-research:migrate-server my-project
```
