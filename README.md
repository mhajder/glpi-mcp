<div align="center">

# GLPI MCP Server

**Let AI agents work with GLPI tickets, assets and the knowledge base through the Model Context Protocol**

[![PyPI](https://img.shields.io/pypi/v/glpi-mcp-server?logo=pypi&logoColor=white)](https://pypi.org/project/glpi-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/glpi-mcp-server?logo=python&logoColor=white)](https://pypi.org/project/glpi-mcp-server/)
[![License](https://img.shields.io/github/license/mhajder/glpi-mcp)](https://github.com/mhajder/glpi-mcp/blob/main/LICENSE)

[![Pre-commit Checks](https://github.com/mhajder/glpi-mcp/actions/workflows/pre-commit.yml/badge.svg)](https://github.com/mhajder/glpi-mcp/actions/workflows/pre-commit.yml)
[![Docker](https://img.shields.io/badge/ghcr.io-glpi--mcp-2496ED?logo=docker&logoColor=white)](https://github.com/mhajder/glpi-mcp/pkgs/container/glpi-mcp)
[![MCP Registry](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fregistry.modelcontextprotocol.io%2Fv0.1%2Fservers%2Fio.github.mhajder%252Fglpi-mcp%2Fversions%2Flatest&query=%24.server.version&label=MCP%20Registry&color=6E56CF)](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.mhajder%2Fglpi-mcp/versions/latest)

[Installation](https://github.com/mhajder/glpi-mcp#-installation) · [Tools](https://github.com/mhajder/glpi-mcp#-available-tools) · [Configuration](https://github.com/mhajder/glpi-mcp#-configuration) · [Security](https://github.com/mhajder/glpi-mcp#-security--safety-features)

</div>

<!-- mcp-name: io.github.mhajder/glpi-mcp -->

GLPI MCP Server is a Python-based Model Context Protocol (MCP) server for the [GLPI](https://glpi-project.org/) IT service management platform. It talks to GLPI's **High-Level API** (`/api.php`, OAuth2) and gives AI assistants and automation tools access to tickets, changes and problems (with their timeline and team), assets, users, groups, entities, the knowledge base and documents, plus generic tools that reach every endpoint GLPI exposes. It supports read and write operations, with read-only and dry-run modes, per-user GLPI access and OAuth login for remote MCP clients.

## ✨ Features

### Core Features

- Search tickets, changes and problems by status, type, priority, category, entity, dates, text or any RSQL filter
- Read the full timeline of an item: followups, tasks, solutions, approvals and documents
- Search and read assets of every type (including custom assets), with financial information
- Find users, groups, entities and profiles
- Search and read knowledge base articles; download documents
- Discover every endpoint and schema from GLPI's own OpenAPI document, so nothing is hard-coded to one GLPI version

### Management Operations

- Create, update and delete tickets, changes and problems (if enabled)
- Add followups, tasks, solutions and approval requests
- Assign technicians and groups, add requesters and observers
- Create, update and delete assets and knowledge base articles
- Create, update and delete items at any endpoint with the generic tools

### Advanced Capabilities

- Supports GLPI 11.0.6+ (API 2.3) and GLPI 12 (API 3.0, or 2.3 if pinned); the version is detected and pinned in every request
- OAuth password grant with token caching and renewal
- Per-user access: token passthrough, or sign-in with GLPI accounts (OAuth login) for remote MCP clients
- OIDC login through an external identity provider, or a static bearer token
- Read-only mode, dry-run mode and tag-based tool filtering
- HTML rich text converted to plain text to save tokens
- Rate limiting, response size limit and optional tool-search transform
- Multiple transport options (STDIO, SSE, HTTP) and optional Sentry integration

## 🚀 Installation

### Prerequisites

- Python 3.11 to 3.15
- GLPI **11.0.6 or newer** (including 12.x). GLPI 10 and GLPI 11.0.0-11.0.5 are not
  supported: they lack High-Level API 2.3
- The API enabled in **Setup > General > API** ("Enable API")
- An OAuth client (see [GLPI setup](#glpi-setup))

### Quick Install from PyPI

The easiest way to get started is to install from PyPI:

```sh
# Using UV (recommended)
uvx glpi-mcp-server

# Or using pip
pip install glpi-mcp-server
```

Remember to configure the environment variables for your GLPI instance before running the server:

```sh
# Create environment configuration
export GLPI_URL=https://glpi.example.com
export GLPI_CLIENT_ID=your-client-id
export GLPI_CLIENT_SECRET=your-client-secret
export GLPI_USERNAME=glpi-mcp
export GLPI_PASSWORD=change-me
```

### Install from Source

1. Clone the repository:

```sh
git clone https://github.com/mhajder/glpi-mcp.git
cd glpi-mcp
```

2. Install dependencies:

```sh
# Using UV (recommended)
uv sync

# Or using pip
pip install -e .
```

3. Configure environment variables:

```sh
cp .env.example .env
# Edit .env with your GLPI URL and OAuth credentials
```

4. Run the server:

```sh
# Using UV (recommended)
uv run glpi-mcp

# Or using the installed command directly
glpi-mcp
```

### Using Docker

A Docker image is available on GitHub Packages for easy deployment. It runs the
HTTP Streamable transport on port 8000 (`/mcp`).

```sh
docker pull ghcr.io/mhajder/glpi-mcp:latest
```

When [GLPI login or OIDC](#-authentication-for-mcp-clients) is enabled, mount a
volume on `/data` so OAuth client registrations survive container recreation.

#### Open WebUI

Open WebUI 0.6.31 and later connect to the image directly. In Open WebUI, go to
**Admin Settings > Integrations > External Tool Servers**, add a connection of
type **MCP (Streamable HTTP)** with the URL `http://<host>:8000/mcp`, and set
Bearer authentication to your `MCP_HTTP_BEARER_TOKEN`.

With [GLPI login or OIDC](#-authentication-for-mcp-clients) enabled, use the
public URL + `/mcp` and choose **OAuth 2.1** authentication instead; Open WebUI
registers itself as a client, so if you restrict redirect URIs, include your
Open WebUI URL.

### Development Setup

For development with additional tools:

```sh
# Clone and install with development dependencies
git clone https://github.com/mhajder/glpi-mcp.git
cd glpi-mcp
uv sync --group dev

# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/

# Run linting and formatting
uv run ruff check .
uv run ruff format .

# Run type checking
uv run ty check .

# Setup prek hooks
uv run prek install
```

## 🔧 Configuration

### GLPI setup

1. **Enable the API**: Setup > General > API > "Enable API".
2. **Create an OAuth client**: Setup > OAuth clients > Add.
   - Grants: **Password** (for `GLPI_USERNAME`/`GLPI_PASSWORD`). Add
     **Authorization Code** for [GLPI login](#glpi-login-users-sign-in-with-glpi).
   - Scopes: **api** and **user**. Add **graphql** for `glpi_graphql_query`.
   - For GLPI login, set the redirect URI to `GLPI_OAUTH_BASE_URL` + `GLPI_OAUTH_REDIRECT_PATH`.
   - Copy the generated client ID and secret.
3. **Create a GLPI user** for the server (for example `glpi-mcp`) with a profile
   that has just the rights the tools need. Every tool call acts as this user,
   unless per-user access is enabled.

### Environment Variables

```env
# GLPI Connection Details
# GLPI root URL, without /api.php
GLPI_URL=https://glpi.example.com

# OAuth client from Setup > OAuth clients
GLPI_CLIENT_ID=your-client-id
GLPI_CLIENT_SECRET=your-client-secret

# GLPI account the tools act as (OAuth password grant)
GLPI_USERNAME=glpi-mcp
GLPI_PASSWORD=change-me

# OAuth scopes, space- or comma-separated (default: api user)
GLPI_SCOPES=api user

# High-Level API version: auto (default), 2.3 or 3.0
GLPI_API_VERSION=auto

# Optional: entity and profile to work in
# GLPI_ENTITY=0
# GLPI_ENTITY_RECURSIVE=true
# GLPI_PROFILE=4

# SSL Configuration
GLPI_VERIFY_SSL=true
GLPI_TIMEOUT=30

# Per-user GLPI access (HTTP transports only; pick at most one)
# GLPI_TOKEN_PASSTHROUGH=false
# GLPI_OAUTH_LOGIN=false
# GLPI_OAUTH_BASE_URL=https://glpi-mcp.example.com
# GLPI_OAUTH_REDIRECT_PATH=/auth/callback
# GLPI_OAUTH_ALLOWED_REDIRECT_URIS=

# Read-Only Mode
READ_ONLY_MODE=false

# Dry-Run Mode
DRY_RUN_MODE=false

# Disabled Tags
# Example: DISABLED_TAGS=assets,knowledgebase,graphql
DISABLED_TAGS=

# Response Size Limit (bytes; empty = unlimited)
# RESPONSE_MAX_SIZE=500000

# Logging Configuration
LOG_LEVEL=INFO

# Rate Limiting
RATE_LIMIT_ENABLED=false
RATE_LIMIT_MAX_REQUESTS=60
RATE_LIMIT_WINDOW_MINUTES=1

# Tool Search Transform (Optional)
TOOL_SEARCH_ENABLED=false
TOOL_SEARCH_STRATEGY=bm25
TOOL_SEARCH_MAX_RESULTS=5

# Sentry Error Tracking (Optional)
# SENTRY_DSN=https://your-key@o12345.ingest.us.sentry.io/6789

# MCP Transport Configuration
# 'stdio' (default), 'sse' or 'http' ('streamable-http' is an alias for 'http')
MCP_TRANSPORT=stdio
# MCP_HTTP_HOST=127.0.0.1
# MCP_HTTP_PORT=8000
# MCP_HTTP_BEARER_TOKEN=

# OIDC / OAuth Authentication (optional, for remote hosting)
# OIDC_CONFIG_URL=https://id.example.com/.well-known/openid-configuration
# OIDC_CLIENT_ID=
# OIDC_CLIENT_SECRET=
# OIDC_BASE_URL=https://glpi-mcp.example.com
```

See [.env.example](.env.example) for every setting with comments.

### API versions

GLPI lists the High-Level API versions it offers at `GET /api.php/`. On first use
the server reads that list and pins one version in every request path
(`/api.php/v2.3/...` or `/api.php/v3.0/...`), because GLPI silently routes an
unknown version to its newest one.

| GLPI | Versions offered | `GLPI_API_VERSION=auto` picks |
|---|---|---|
| 10.x, 11.0.0-11.0.5 | none / 2.0-2.2 | not supported |
| 11.0.6+ | 2.3 | 2.3 |
| 12.x | 2.3, 3.0 | 3.0 |

Set `GLPI_API_VERSION=2.3` to keep GLPI 12 on the 2.3 schemas (for example while
migrating other integrations). The tools that depend on a schema difference
handle both: knowledge base articles are filed under `categories` on 2.3 and
under parent articles (`parents`) on 3.0.

### Entity and profile

GLPI scopes every API session to an active profile and entities. By default
these are the user's defaults. `GLPI_ENTITY`, `GLPI_ENTITY_RECURSIVE` and
`GLPI_PROFILE` send the `GLPI-Entity`, `GLPI-Entity-Recursive` and `GLPI-Profile`
headers with every request to change them. `glpi_whoami` shows the active ones
and `glpi_get_entity_tree` lists the entities available.

## 🧰 Available Tools

### Server & Session

- `glpi_server_info`: API versions GLPI offers, the pinned version, auth mode and safety modes
- `glpi_whoami`: The GLPI user the tools act as, with the active profile and entities
- `glpi_get_entity_tree`: Entities the current user can work in

### Tickets, Changes & Problems

All take `itil_type`: `Ticket` (default), `Change` or `Problem`.

- `glpi_search_itil`: Search by status, type, priority, category, entity, dates, text or filter
- `glpi_get_itil`: One item with its description and team
- `glpi_get_itil_timeline`: Followups, tasks, solutions, approvals and documents
- `glpi_create_itil` / `glpi_update_itil` / `glpi_delete_itil`: Create, update, delete
- `glpi_add_itil_followup`: Add a followup (public or private)
- `glpi_add_itil_task`: Add a task with state, time spent, technician and planning
- `glpi_add_itil_solution`: Add a solution
- `glpi_request_itil_validation`: Ask a user or group to approve a ticket or change
- `glpi_list_itil_team`: Requesters, assignees and observers
- `glpi_add_itil_team_member` / `glpi_remove_itil_team_member`: Assign or remove users, groups and suppliers

### Assets

- `glpi_list_asset_types`: Asset types of this GLPI, including custom assets
- `glpi_search_assets`: Search one asset type, or all at once, by name, serial, inventory number or filter
- `glpi_get_asset`: One asset, optionally with financial information
- `glpi_create_asset` / `glpi_update_asset` / `glpi_delete_asset`: Create, update, delete

### Users, Groups, Entities & Profiles

- `glpi_search_users` / `glpi_get_user`: Find users; get one by ID or login
- `glpi_search_groups` / `glpi_get_group`: Find groups; get one by ID
- `glpi_search_entities` / `glpi_get_entity`: Find entities; get one by ID
- `glpi_list_profiles`: Profiles (sets of rights)

### Knowledge Base

- `glpi_search_kb_articles` / `glpi_get_kb_article`: Search and read articles
- `glpi_create_kb_article` / `glpi_update_kb_article`: Create and update articles
- `glpi_list_kb_categories`: Categories (API 2.3), or top-level articles (API 3.0)

### Documents

- `glpi_search_documents` / `glpi_get_document`: Find documents and read their metadata
- `glpi_download_document`: Download a file (text, or base64 for binary files)

The High-Level API has no file upload, so documents are uploaded through GLPI itself.

### Generic

- `glpi_list_endpoints`: Every endpoint path with its methods, from GLPI's OpenAPI document
- `glpi_describe_schema`: Properties, types and enums of a schema, for filters and writes
- `glpi_list_items`: List any collection with filters, sorting, paging and field selection
- `glpi_get_item`: Get any item or resource
- `glpi_create_item` / `glpi_update_item` / `glpi_delete_item`: Write to any endpoint
- `glpi_graphql_query`: Read-only GraphQL query (needs the `graphql` scope)

### Filtering

List tools accept `filters`, a dict turned into GLPI's RSQL, and `filter`, raw RSQL.
Both are AND-ed:

```json
{
  "filters": {"status.id": [1, 2], "name__ilike": "*printer*", "entity.id": 0},
  "filter": "priority=ge=4"
}
```

Suffixes: `__ne`, `__in`, `__out`, `__lt`, `__le`, `__gt`, `__ge`, `__like`,
`__ilike` (`*` is the wildcard), `__isnull`, `__isnotnull`, `__empty`,
`__notempty`. A list means "any of"; `None` means "is null". Nested properties
use dot paths (`status.id`, `entity.id`). `glpi_describe_schema` lists the
properties of each schema. Computed properties, such as the `team` of a ticket,
cannot be filtered on; GLPI answers HTTP 500.

## 🔐 Authentication for MCP Clients

How MCP clients authenticate to this server (HTTP transports), and as whom the
tools then act in GLPI. Pick one:

| Mode | Setting | MCP client sends | Tools act as |
|---|---|---|---|
| None | - | nothing | `GLPI_USERNAME` |
| Static bearer token | `MCP_HTTP_BEARER_TOKEN` | the shared token | `GLPI_USERNAME` |
| OIDC | `OIDC_*` | OAuth login with your identity provider | `GLPI_USERNAME` |
| GLPI login | `GLPI_OAUTH_LOGIN=true` | OAuth login with their GLPI account | each user |
| Token passthrough | `GLPI_TOKEN_PASSTHROUGH=true` | their own GLPI access token | each user |

The per-user modes (GLPI login and token passthrough) cannot be combined with each
other, with OIDC or with `MCP_HTTP_BEARER_TOKEN`; the server refuses to start.

### GLPI login: users sign in with GLPI

Many MCP clients (Claude, Open WebUI, remote connectors) can only authenticate
over OAuth with Dynamic Client Registration. With `GLPI_OAUTH_LOGIN=true` the
server offers that OAuth interface and brokers it to GLPI's own OAuth server
(`/api.php/authorize`, `/api.php/token`) using FastMCP's `OAuthProxy`. Users
approve the MCP client on this server's consent page, sign in on the GLPI login
page and click **Accept** on GLPI's "wants to access your GLPI account" page.
Every tool call then runs with that user's own GLPI token, rights and entities.
GLPI's refresh tokens are used when the access token expires.

1. On the OAuth client in GLPI, allow the **Authorization Code** grant, the
   `api` and `user` scopes, and set the redirect URI to
   `GLPI_OAUTH_BASE_URL` + `GLPI_OAUTH_REDIRECT_PATH`, for example
   `https://glpi-mcp.example.com/auth/callback`.
2. Configure the server:

```env
MCP_TRANSPORT=http
MCP_HTTP_HOST=0.0.0.0
GLPI_URL=https://glpi.example.com
GLPI_CLIENT_ID=your-client-id
GLPI_CLIENT_SECRET=your-client-secret
GLPI_OAUTH_LOGIN=true
# Public HTTPS URL of this server
GLPI_OAUTH_BASE_URL=https://glpi-mcp.example.com
# Optional
GLPI_OAUTH_REDIRECT_PATH=/auth/callback
GLPI_OAUTH_ALLOWED_REDIRECT_URIS=https://claude.ai/*,https://openwebui.example.com/*
```

`GLPI_USERNAME` and `GLPI_PASSWORD` are not needed. `GLPI_OAUTH_ALLOWED_REDIRECT_URIS`
restricts which MCP clients may complete the flow; leaving it unset accepts any
redirect URI a client registers.

### Token passthrough

With `GLPI_TOKEN_PASSTHROUGH=true`, each MCP client sends its own GLPI OAuth
access token as `Authorization: Bearer <token>`. The server checks it against
GLPI (`GET /session`, cached for a minute) and forwards it on every call, so each
client acts as its own GLPI user. The server's own credentials are never used.
The client is responsible for obtaining and refreshing its token.

### OIDC / OAuth Authentication (Optional)

For OAuth-only MCP clients when the tools should act as one service account, the
server can act as an OAuth interface in front of an existing OIDC identity
provider (Authentik, Keycloak, PocketID, Auth0, Entra ID, ...) using FastMCP's
`OIDCProxy`. No GLPI credential ever reaches the client.

Create a **confidential** client on your identity provider with the redirect URI
set to `OIDC_BASE_URL` + `OIDC_REDIRECT_PATH`, for example
`https://glpi-mcp.example.com/auth/callback`.

```env
MCP_TRANSPORT=http
MCP_HTTP_HOST=0.0.0.0
MCP_HTTP_PORT=8000

# All four are required to enable OIDC
OIDC_CONFIG_URL=https://id.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
# Public URL where this server is reachable (HTTPS except on localhost)
OIDC_BASE_URL=https://glpi-mcp.example.com

# Optional settings
OIDC_REDIRECT_PATH=/auth/callback
OIDC_REQUIRED_SCOPES=openid,profile
OIDC_ALLOWED_REDIRECT_URIS=https://example.com/*
OIDC_VERIFY_ID_TOKEN=false
OIDC_FORWARD_RESOURCE=false
```

A partially configured OIDC setup is ignored with a warning. When OIDC is
configured it takes precedence over `MCP_HTTP_BEARER_TOKEN`.

- `OIDC_VERIFY_ID_TOKEN=true` verifies the `id_token` instead of the access
  token, for identity providers that issue opaque access tokens.
- `OIDC_FORWARD_RESOURCE` is off by default because identity providers without
  RFC 8707 support reject the authorization request with `invalid_request`.

### Persisting OAuth state

With GLPI login or OIDC, client registrations and encrypted tokens are stored on
disk under FastMCP's data directory. If it is not persistent, every restart forces
all clients to register and sign in again. The Docker image sets
`FASTMCP_HOME=/data`, so mount a volume there:

```sh
docker run -v glpi-mcp-data:/data --env-file .env ghcr.io/mhajder/glpi-mcp:latest
```

### Running behind a reverse proxy

`GLPI_OAUTH_BASE_URL` / `OIDC_BASE_URL` must be the externally reachable HTTPS URL,
and the proxy must forward the `Host` header unchanged, otherwise the OAuth
metadata this server advertises will point at the wrong host.

## 🔒 Security & Safety Features

### Read-Only Mode

The server supports a read-only mode that hides every write tool:

```env
READ_ONLY_MODE=true
```

### Dry-Run Mode

Write tools stay available but only return the request they would send
(method, endpoint and body); nothing is changed in GLPI:

```env
DRY_RUN_MODE=true
```

### Tag-Based Tool Filtering

You can disable specific categories of tools by setting disabled tags:

```env
DISABLED_TAGS=assets,knowledgebase,graphql
```

Tags: `session`, `generic`, `graphql`, `itil`, `tickets`, `assets`,
`administration`, `users`, `groups`, `entities`, `profiles`, `knowledgebase`,
`documents`, `write`, `read-only`.

### Safe writes

- A write whose request was sent but whose answer never arrived (timeout, dropped
  connection) is reported as possibly applied, never retried blindly.
- `5xx` answers to a create are not retried, as GLPI may have created the item.
- A redirect that would turn a write into a GET is reported as not applied.
- Deletes move items to the trash unless `force=true` is given. A delete retried
  after a server error that then finds the item gone is reported as deleted.
- `GLPI_TIMEOUT` is the total for one request: version lookup, token, the
  request and its retries share it.
- Generic tools reject endpoint paths that leave the API or name the OAuth endpoints.

### Response Size Limit

```env
RESPONSE_MAX_SIZE=500000
```

Caps every tool response, so a large listing cannot flood the model's context.

### Tool Search for Large Toolsets

FastMCP tool search can reduce prompt size for servers with many tools.
When enabled, `list_tools` returns two synthetic tools:

- `search_tools`: Finds matching tools and returns their full schemas
- `call_tool`: Executes any discovered tool by name

```env
TOOL_SEARCH_ENABLED=true
TOOL_SEARCH_STRATEGY=bm25      # bm25 or regex
TOOL_SEARCH_MAX_RESULTS=8      # optional, default is 5
```

Tool search respects existing visibility controls (read-only mode and
disabled tags).

### Rate Limiting

Requests are limited per client using a sliding window. With per-user access,
each GLPI user gets their own window.

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_MAX_REQUESTS=60    # Maximum requests allowed per window (default: 60)
RATE_LIMIT_WINDOW_MINUTES=1   # Window size in minutes
```

### Sentry Error Tracking & Monitoring (Optional)

The server optionally supports **Sentry** for error tracking and performance
monitoring. Install the optional dependency:

```sh
uv sync --extra sentry
```

```env
# Required: Sentry DSN for your project
SENTRY_DSN=https://your-key@o12345.ingest.us.sentry.io/6789

# Optional settings
SENTRY_TRACES_SAMPLE_RATE=1.0
# When true, tool inputs and outputs (ticket contents, users...) are sent to Sentry.
SENTRY_SEND_DEFAULT_PII=false
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=1.2.3
SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0
SENTRY_PROFILE_LIFECYCLE=trace
SENTRY_ENABLE_LOGS=true
```

If `SENTRY_DSN` is not set, no monitoring data is collected.

### SSL/TLS Configuration

```env
GLPI_VERIFY_SSL=true    # Verify GLPI's certificate against the system trust store
GLPI_TIMEOUT=30         # Total seconds for one request, retries included
```

### Transport Configuration

#### STDIO Transport (Default)

```env
MCP_TRANSPORT=stdio
```

#### HTTP SSE Transport (Server-Sent Events)

```env
MCP_TRANSPORT=sse
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_HTTP_BEARER_TOKEN=your-secret-token  # Optional authentication token
```

#### HTTP Streamable Transport

```env
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_HTTP_BEARER_TOKEN=your-secret-token  # Optional authentication token
```

```sh
curl -H "Authorization: Bearer your-secret-token" \
     -H "Accept: application/json, text/event-stream" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://localhost:8000/mcp
```

#### Health Endpoint

Both HTTP transports (`sse` and `http`) serve `GET /health`, which answers `200 ok`
when the server is handling requests. It needs no bearer token or OAuth login and
does not contact GLPI, so a GLPI outage does not mark the MCP server unhealthy.
The Docker image's `HEALTHCHECK` uses it:

```sh
curl http://localhost:8000/health
```

For more information on FastMCP transports, see the [FastMCP documentation](https://gofastmcp.com/deployment/running-server#transport-protocols).

## 🤝 Contributing

Contributions are welcome. See [CONTRIBUTING.md](https://github.com/mhajder/glpi-mcp/blob/main/CONTRIBUTING.md) for the dev setup, checks and commit message format, and use the [issue templates](https://github.com/mhajder/glpi-mcp/issues/new/choose) for bugs and feature requests.

Found a security problem? Please report it privately as described in [SECURITY.md](https://github.com/mhajder/glpi-mcp/blob/main/SECURITY.md).

## 📜 License

MIT License - see [LICENSE](https://github.com/mhajder/glpi-mcp/blob/main/LICENSE) for details.
