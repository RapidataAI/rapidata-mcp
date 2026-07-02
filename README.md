# Rapidata MCP Server

A **hosted, multi-tenant [MCP](https://modelcontextprotocol.io) server** that exposes Rapidata
human-labeling tasks to customers' own AI agents — Claude Desktop, Cursor, IDE assistants, or any
custom MCP client. Point your agent at the server, authenticate once in the browser, and it can
create, run, monitor, and read the results of labeling tasks through tool calls.

This is **Stage 1** of the Rapidata MCP product, and it ships directly as a **remote** server over
the **Streamable-HTTP** transport. There is no local/stdio product.

Public endpoint: **`https://mcp.rapidata.ai/mcp`**

## Tools

Tasks run as **jobs** on Rapidata: a create tool produces a **job definition** (a draft template), and `start_job` runs that definition on the **global audience**, producing a **job** you then poll and read.

| Tool | Purpose |
|---|---|
| `create_classification_task` | Humans pick one option per item. Creates a draft job definition — **no spend**. |
| `create_comparison_task` | Pairwise comparison (choose between two). Creates a draft job definition — **no spend**. |
| `start_job` | Run a draft job definition on the global audience — **the single step that spends.** |
| `get_job_status` | Current status of a job. |
| `get_job_results` | Partial once paused, final once complete; **never blocks.** |
| `list_jobs` | Your most recent jobs. |
| `pause_job` | Stop collecting (and spending). |

Both create tools take the same core parameters: the **question** (`instruction`), optional per-datapoint **`contexts`** (text shown alongside the question), **`responses_per_datapoint`**, and — classification only — the **`answer_options`**. Media is given as URLs: `comparison_pairs` (two per pair, required) for comparison, and `datapoint_urls` for classification — where it is **optional**: omit it and the crowd answers the instruction on its own against a generic placeholder image.

Two invariants hold across the tool layer:

- **Spending is explicit.** Creating a task never spends. `create_*` returns a draft with
  `confirmation_required` and `total_responses` (datapoints × responses_per_datapoint) as the honest
  cost driver; `start_job` is the only step that spends. The create response instructs the agent to
  confirm the cost with the user and review the `details_url` before starting.
- **Results never block.** `get_job_results` returns the final results once complete, the partial
  snapshot if the job is paused, and a pollable `result_status` (`not_started`, `collecting`,
  `manual_review`, …) while it is still processing. Per-annotator detail is dropped unless
  `include_details=true`, and the datapoint list is capped (`max_datapoints`).

## Authentication

The server is an **OAuth 2.0 Protected Resource** ([RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728)).
Each customer authenticates their MCP client once; every request then carries a bearer JWT that the
server validates and uses to scope all data access to that customer. A compliant MCP client
(Claude Desktop, Cursor, the MCP Inspector, …) drives the whole flow automatically once you give it
the server URL — the steps below are what happens under the hood.

### 1. Discover the authorization server

An unauthenticated request gets a `401` with:

```
WWW-Authenticate: Bearer ..., resource_metadata="https://mcp.rapidata.ai/.well-known/oauth-protected-resource"
```

Fetching that metadata document returns:

```json
{
  "resource": "https://mcp.rapidata.ai",
  "authorization_servers": ["https://auth.rapidata.ai"],
  "scopes_supported": ["openid", "profile", "email", "roles", "offline_access", "mcp"],
  "bearer_methods_supported": ["header"]
}
```

So the authorization server is **`https://auth.rapidata.ai`**.

### 2. Register a client (Dynamic Client Registration)

Rapidata's authorization server supports **open Dynamic Client Registration**
([RFC 7591](https://datatracker.ietf.org/doc/html/rfc7591)). Your MCP client self-registers a public
PKCE client at:

```
POST https://auth.rapidata.ai/client/register
```

> **Interim note.** Until the sibling identity-server change ships, the authorization-server
> discovery document (`https://auth.rapidata.ai/.well-known/oauth-authorization-server`) does **not**
> yet advertise a `registration_endpoint`. Use the registration URL above explicitly. Clients that
> rely solely on discovery to find the registration endpoint will need it configured manually until
> then.

### 3. Authorization Code + PKCE

Run the standard authorization-code flow with PKCE:

- **Scopes:** request `openid email roles offline_access mcp`.
  (`offline_access` yields a refresh token so the client can stay connected without re-prompting;
  `roles` and `email` identify the customer; `mcp` carries the MCP server as an OAuth resource so
  the authorization server accepts the RFC 8707 resource indicator the client sends for this server.)
- One-time browser consent, then the client exchanges the code (with its PKCE verifier) for an
  access token and a refresh token.

The resulting access token's `sub` claim is the Rapidata **CustomerId**. The server forwards the raw
token to the Rapidata API gateway, which scopes orders/assets to that customer.

### What the server validates

For every request the server:

1. Reads the `Authorization: Bearer <jwt>` header.
2. Verifies the JWT signature against the JWKS published by `https://auth.rapidata.ai` (resolved via
   its OIDC discovery document), checks the issuer, and checks expiry.
3. Extracts `sub` as the CustomerId and builds a per-request, token-scoped Rapidata client.

A valid, unexpired, correctly-issued token is required; no specific scope is *enforced* beyond that
(the metadata advertises which scopes a client should request).

## Connecting a client

### Claude Desktop / Cursor (and other MCP clients with remote support)

Add a remote MCP server pointing at `https://mcp.rapidata.ai/mcp`. The client performs discovery,
registration, and the browser consent flow for you.

### MCP Inspector (for testing)

```bash
npx @modelcontextprotocol/inspector
```

Enter the URL `https://mcp.rapidata.ai/mcp`, transport **Streamable HTTP**, and complete the OAuth
prompt.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RAPIDATA_MCP_RESOURCE_URL` | `https://mcp.rapidata.ai` | This server's public resource identifier. |
| `RAPIDATA_MCP_ISSUER_URL` | `https://auth.rapidata.ai` | The authorization server (issuer). |
| `RAPIDATA_MCP_SCOPES` | `openid profile email roles offline_access` | Advertised `scopes_supported`. |
| `RAPIDATA_ENVIRONMENT` | _(SDK default)_ | Rapidata SDK environment to target. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address. |
| `LOG_LEVEL` | `INFO` | Logging level. |
| `RAPIDATA_MCP_AUTH_DISABLED` | _(unset)_ | **Local dev only.** Serve without auth using ambient SDK credentials (`RAPIDATA_CLIENT_ID` / `RAPIDATA_CLIENT_SECRET`). |

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `POST/GET/DELETE /mcp` | Bearer JWT | The MCP Streamable-HTTP endpoint. |
| `GET /.well-known/oauth-protected-resource` | none | RFC 9728 protected-resource metadata. |
| `GET /health` | none | Liveness/readiness probe → `{"status":"ok"}`. |

## Local development

```bash
uv sync --group dev

# Serve without auth, using ambient SDK credentials, for local testing:
RAPIDATA_MCP_AUTH_DISABLED=1 \
RAPIDATA_CLIENT_ID=... RAPIDATA_CLIENT_SECRET=... \
uv run rapidata-mcp
# → http://localhost:8000/mcp  (no OAuth; do NOT use in production)
```

Type-check and lint:

```bash
uv run pyright
uv run ruff check src
```

## Docker

```bash
docker build -t rapidata-mcp .
docker run -p 8000:8000 \
  -e RAPIDATA_MCP_RESOURCE_URL=https://mcp.rapidata.ai \
  -e RAPIDATA_MCP_ISSUER_URL=https://auth.rapidata.ai \
  rapidata-mcp
```

## Architecture

```
client ──Bearer JWT──▶ /mcp ──▶ RequireAuth ─▶ FastMCP tools
                         │           │              │
                         │      JWTVerifier      provider_factory()
                         │   (JWKS, iss, exp)    get_access_token() ─▶ TokenClientProvider
                         │                              │
                         ▼                              ▼
          /.well-known/oauth-protected-resource   RapidataClient(token=…) ─▶ Rapidata API gateway
```

- `tools.py` — the 7 tools; transport- and auth-agnostic, resolved through a `ClientProvider`.
- `auth.py` — the client-resolution seam (`TokenClientProvider` for hosted, `EnvClientProvider` for
  local dev).
- `token_verifier.py` — JWT validation against the issuer's JWKS (`TokenVerifier`).
- `server.py` — the Streamable-HTTP + OAuth resource-server shell and per-request client wiring.

## Scope (Stage 1)

URL datapoints only (no local upload), classification + comparison only, default audience (no
targeting/filters), no validation sets / signals / flows / leaderboards. Those are later stages. The
order-manager API used here is marked deprecated in the SDK in favour of `job` + `audience`; kept for
Stage 1 simplicity, migration is a sensible follow-up.
