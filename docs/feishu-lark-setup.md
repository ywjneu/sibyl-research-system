# Feishu/Lark Cloud Sync Setup

Sibyl can sync research progress, papers, and data tables to Feishu (飞书) / Lark cloud workspace.

## Architecture

Sibyl uses a **dual-MCP architecture** for Feishu:

| MCP Server | Auth | Purpose |
|------------|------|---------|
| `lark` (Official) | Tenant access token | Bitable (multidimensional tables), instant messaging |
| `feishu` (Community) | User OAuth token | Document creation, folders, native tables |

Both are needed for full functionality because the official Lark MCP focuses on Bitable/IM, while document operations require the community Feishu MCP.

## Setup

### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/) (or [Lark Developer](https://open.larksuite.com/))
2. Create a new app
3. Note your **App ID** and **App Secret**
4. Configure permissions:
   - `docs:doc` — Document read/write
   - `drive:drive` — File management
   - `bitable:app` — Bitable read/write
   - `im:message` — Send messages

### 2. Install Official Lark MCP

```bash
npm install -g @larksuiteoapi/lark-mcp
```

Add to `~/.mcp.json`:

```json
{
  "mcpServers": {
    "lark": {
      "command": "npx",
      "args": ["-y", "@larksuiteoapi/lark-mcp"],
      "env": {
        "LARK_APP_ID": "your-app-id",
        "LARK_APP_SECRET": "your-app-secret"
      }
    }
  }
}
```

### 3. Install Community Feishu MCP

Search for `feishu-mcp` community implementations. Add to `~/.mcp.json`:

```json
{
  "mcpServers": {
    "feishu": {
      "command": "feishu-mcp",
      "args": [],
      "env": {
        "FEISHU_USER_ACCESS_TOKEN": "your-user-access-token"
      }
    }
  }
}
```

The user access token requires OAuth authorization flow. Refer to the Feishu MCP documentation for the token acquisition process.

### 4. Enable in Config

```yaml
lark_enabled: true    # Default: true
```

## How Sync Works

1. After each eligible pipeline stage, `cli_record()` appends a trigger to `{workspace}/lark_sync/pending_sync.jsonl` and returns `sync_requested: true`
2. The main Claude session launches `sibyl-lark-sync` in the background (non-blocking)
3. The `sibyl-lark-sync` skill:
   - Creates/updates a Feishu document for the research project
   - Uploads experiment results as native tables (NOT code blocks)
   - Syncs research diary and stage summaries
   - Uploads compiled PDF papers
4. Token registry stored in `{workspace}/lark_sync/registry.json`
5. Latest result is stored in `{workspace}/lark_sync/sync_status.json`

### Document Rules

- **Tables must use `create_feishu_table`** (native tables), NOT code block rendering
- Documents are split by iteration, each volume ≤ 20KB
- Each `# Iteration` section becomes a separate document volume

## Disabling Sync

```yaml
lark_enabled: false
```

When disabled, background sync triggers are skipped entirely. All research data remains available locally in the workspace.

## Manual Sync

Trigger sync at any time:

```bash
/sibyl-research:sync <project>
```
