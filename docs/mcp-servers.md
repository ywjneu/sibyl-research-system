# MCP Server Dependencies

Sibyl relies on [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers for external integrations. All MCP servers are registered in `~/.mcp.json`.

## Overview

| MCP Server | Required | Purpose | Install |
|------------|----------|---------|---------|
| [SSH MCP](#ssh-mcp-server) | Yes | Remote GPU execution & file transfer | Claude Code built-in |
| [arXiv MCP](#arxiv-mcp-server) | Yes | Academic paper search | pip |
| [Google Scholar MCP](#google-scholar-mcp) | Recommended | Citation & author search | pip |
| [Codex MCP](#codex-mcp) | Optional | GPT-5.4 cross-review | npm (OpenAI Codex CLI) |
| [Lark MCP (Official)](#lark-mcp-official) | Optional | Feishu Bitable & messaging | npm |
| [Feishu MCP (Community)](#feishu-mcp-community) | Optional | Feishu documents & folders | pip/npm |
| [bioRxiv MCP](#biorxiv-mcp) | Optional | Biology preprint search | pip |
| [Playwright MCP](#playwright-mcp) | Optional | Web browsing automation | Claude Code plugin |

## SSH MCP Server

**Built into Claude Code** — no separate installation needed.

**Purpose**: Execute commands on remote GPU servers, upload/download files.

**Tools used**: `execute-command`, `upload`, `download`, `list-servers`

**Configuration**: Add your GPU server to `~/.ssh/config`:

```
Host my-gpu-server
    HostName 192.168.1.100
    User your-username
    IdentityFile ~/.ssh/id_rsa
```

Then set `ssh_server: "my-gpu-server"` in your project `config.yaml`.

See [SSH & GPU Setup](ssh-gpu-setup.md) for detailed server-side configuration.

## arXiv MCP Server

**Purpose**: Search and retrieve academic papers from arXiv.

**Tools used**: `search_papers`, `download_paper`, `read_paper`, `list_papers`

**Used by**: Literature search, idea generation agents, comparativist agent

### Install

```bash
pip install arxiv-mcp-server
```

### Configure (`~/.mcp.json`)

```json
{
  "mcpServers": {
    "arxiv-mcp-server": {
      "command": "python",
      "args": ["-m", "arxiv_mcp_server"],
      "env": {}
    }
  }
}
```

## Google Scholar MCP

**Purpose**: Search Google Scholar for papers, get author information.

**Tools used**: `search_google_scholar_key_words`, `search_google_scholar_advanced`, `get_author_info`

**Used by**: Literature search, idea generation agents, comparativist agent

### Install

Search for available Google Scholar MCP implementations on GitHub/npm. Common options:

```bash
# Example (check for latest available package)
pip install google-scholar-mcp
```

### Configure (`~/.mcp.json`)

```json
{
  "mcpServers": {
    "google-scholar": {
      "command": "python",
      "args": ["-m", "google_scholar_mcp"],
      "env": {}
    }
  }
}
```

> **Note**: Google Scholar MCP availability may vary. If unavailable, the system falls back to arXiv + WebSearch for literature discovery.

## Codex MCP

**Purpose**: Independent GPT-5.4 cross-review for idea debate, result analysis, and paper review.

**Tools used**: `codex` (single query), `codex-reply` (multi-turn conversation)

**Used by**: Codex reviewer skill, optional Codex writing mode

### Install

```bash
npm install -g @openai/codex
```

### Configure

1. Set up `~/.codex/config.toml`:

```toml
model = "gpt-5.4"
model_reasoning_effort = "high"
```

2. Set `OPENAI_API_KEY` environment variable.

3. Add to `~/.mcp.json`:

```json
{
  "mcpServers": {
    "codex": {
      "command": "codex",
      "args": ["mcp-server"],
      "env": {
        "OPENAI_API_KEY": "your-key-here"
      }
    }
  }
}
```

See [Codex Integration](codex-integration.md) for full details.

**Disable**: Set `codex_enabled: false` in `config.yaml`.

## Lark MCP (Official)

**Purpose**: Feishu/Lark Bitable (multidimensional tables) and instant messaging.

**Tools used**: `bitable_v1_*`, `im_v1_*`

**Used by**: Lark sync skill (data tables, team notifications)

### Install

```bash
npm install -g @larksuiteoapi/lark-mcp
```

### Configure (`~/.mcp.json`)

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

Requires a Feishu/Lark app with tenant access token. See [Feishu/Lark Setup](feishu-lark-setup.md).

## Feishu MCP (Community)

**Purpose**: Feishu document creation, folder management, native tables.

**Tools used**: `create_feishu_document`, `batch_create_feishu_blocks`, `create_feishu_table`, `create_feishu_folder`, etc.

**Used by**: Lark sync skill (research documents, paper uploads)

### Install

Search for `feishu-mcp` on GitHub/npm for available community implementations.

### Configure (`~/.mcp.json`)

```json
{
  "mcpServers": {
    "feishu": {
      "command": "feishu-mcp",
      "args": [],
      "env": {
        "FEISHU_USER_ACCESS_TOKEN": "your-user-token"
      }
    }
  }
}
```

Requires user OAuth token. See [Feishu/Lark Setup](feishu-lark-setup.md).

> **Important**: Sibyl uses a dual-MCP architecture for Feishu — the official `lark` MCP for Bitable/IM, and the community `feishu` MCP for document operations. Both are needed for full sync functionality.

## bioRxiv MCP

**Purpose**: Search biological and medical preprints.

**Tools used**: `search_preprints`, `get_preprint`

**Used by**: Innovator, interdisciplinary, and contrarian agents (for cross-domain inspiration)

### Configure (`~/.mcp.json`)

```json
{
  "mcpServers": {
    "claude_ai_bioRxiv": {
      "command": "python",
      "args": ["-m", "biorxiv_mcp"],
      "env": {}
    }
  }
}
```

## Playwright MCP

**Purpose**: Web browsing automation for research (accessing websites, reading documentation).

**Used by**: Literature search (web sources), experiment agents (documentation lookup)

### Install

Available as a Claude Code plugin. Install via Claude Code settings or:

```bash
claude mcp add playwright
```

## Minimal `~/.mcp.json` Example

A minimal configuration with only required servers:

```json
{
  "mcpServers": {
    "arxiv-mcp-server": {
      "command": "python",
      "args": ["-m", "arxiv_mcp_server"]
    }
  }
}
```

SSH MCP is built into Claude Code and does not need an entry in `~/.mcp.json`.

## Adapting MCP Servers

If you use different MCP server implementations than those listed above:

1. **Tool name compatibility**: Sibyl's agent prompts reference specific MCP tool names (e.g., `mcp__arxiv-mcp-server__search_papers`). If your MCP server uses different tool names, you'll need to update the corresponding prompt files in `sibyl/prompts/`.

2. **Server name in `~/.mcp.json`**: The server name in `~/.mcp.json` becomes part of the tool name prefix. For example, if you name your arXiv server `my-arxiv`, tools will be called `mcp__my-arxiv__search_papers`. Update prompt references accordingly.

3. **Permission allowlists**: If using `.claude/settings.local.json` for tool permissions, update the allowlist to match your server names.
