# Steam Companion MCP

**A self-hosted MCP server that gives MCP clients and AI assistants a secure, read-only view of one Steam account.**

Ask account-aware questions about your library, backlog, co-op options, achievements, regional prices, and purchase context without giving an assistant your Steam password or session cookies. Steam account access is restricted to the single SteamID64 configured by the server owner.

Steam Companion is designed for a personal deployment: one account, one process, SQLite, and a small Docker memory limit.

## What it can do

The default `core` toolset includes:

- **Library:** summarize, analyze, search, sort, and page through visible owned games; check candidate AppIDs; see recently played games and saved library history.
- **Backlog:** combine your explicit backlog states and local watchlist with separate, clearly labeled activity-based suggestions.
- **Game context:** assemble ownership, playtime, regional offer and catalog details, achievements, aggregate reviews, and current player count. Optional provider failures produce partial results with warnings.
- **Store and wishlist:** read current offers, wishlist pages, and saved price history; price history is local and is not refreshed by a history request.
- **Planning:** get a bounded co-op overlap for games you own, review summaries, and purchase context that combines ownership, offers, wallet settings, watchlists, and observed prices.
- **Account:** confirm the connected Steam identity and check which data providers are enabled.

Set `MCP_TOOLSET=full` to add game comparisons, package analysis, activity and event history, a player profile summary, and guided prompts for choosing a game, evaluating a purchase, planning co-op, reviewing a backlog, and building a sale shortlist.

The MCP interface does not expose tools for changing your Steam account or purchasing games. Some successful reads intentionally record local observations—such as a library snapshot or a tracked price—to build history. Backlog and watchlist edits are made through the authenticated local web page.

## How it handles your data

- Steam OpenID verifies the account. The assistant never chooses a SteamID: requests use the one 17-digit ID in `ALLOWED_STEAM_IDS`.
- OAuth 2.1 authorization code flow uses PKCE. Access tokens are resource-bound; refresh tokens rotate.
- Steam Web API credentials and signing secrets stay on the server. Do not share your Steam password, Steam Guard code, API key, or session cookies.
- Data and observation history are stored in a local SQLite database on the Docker volume. There is no background polling or cloud database.
- Wallet balance is optional, manual, and never compared across currencies.
- Steam Storefront prices and catalog metadata use an unofficial public endpoint. This provider is disabled by default and is labeled as unofficial when enabled.
- Steam privacy settings still apply. A successful login does not make a private library or wishlist visible. Missing ownership data remains unknown where Steam's response cannot establish it.

## Run with Docker Compose

### Requirements

- Docker Engine with Docker Compose
- A domain served over HTTPS, with a reverse proxy in front of the app
- A [Steam Web API key](https://steamcommunity.com/dev/apikey)

### 1. Configure the service

```sh
cp .env.example .env
```

Edit `.env` and set:

| Variable | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Public HTTPS origin, for example `https://steam.example.com` |
| `STEAM_WEB_API_KEY` | Steam Web API key; keep it server-side |
| `ALLOWED_STEAM_IDS` | Exactly one 17-digit SteamID64 |
| `OAUTH_SIGNING_KEY` | Ed25519 private key in PEM format |
| `SESSION_SECRET` | Random secret of at least 32 bytes |
| `STORE_COUNTRY_DEFAULT`, `STORE_LANGUAGE_DEFAULT`, `STORE_CURRENCY_DEFAULT` | Initial regional store preferences |
| `ENABLE_UNOFFICIAL_STOREFRONT` | Set to `true` to enable public Storefront pricing and metadata |
| `MCP_TOOLSET` | `core` by default, or `full` for the additional tools and prompts |

Generate signing material with OpenSSL:

```sh
openssl genpkey -algorithm Ed25519 -out oauth-ed25519.pem
openssl rand -hex 32
```

Put the private key contents in `OAUTH_SIGNING_KEY` and the random hex output in `SESSION_SECRET`. Keep `.env` and `oauth-ed25519.pem` private; both are excluded from Git.

The sample region in `.env.example` is only a starting value. Set the country and currency that match your account's store region.

### 2. Start the service

```sh
docker compose up -d --build
```

Compose stores the database in the `steam_data` volume, binds the app to localhost, enables a read-only container filesystem, and limits the service to 128 MiB of memory. Put the included Caddy or Nginx example behind your HTTPS domain and preserve streaming responses for `/mcp`.

Check the local endpoints:

```sh
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

`/healthz` reports process liveness; `/readyz` also checks local database readiness. Neither endpoint calls Steam.

### 3. Connect an MCP client

Configure your MCP client with the remote Streamable HTTP endpoint:

```text
https://steam.example.com/mcp
```

Complete the OAuth flow in the client. Steam OpenID verifies the account, then the local setup page lets you choose regional preferences and optionally enter a manual wallet balance. The server publishes OAuth protected-resource metadata and supports CIMD client metadata for compatible clients.

For ChatGPT, add the same HTTPS MCP endpoint from Developer Mode and complete the authorization flow. Other MCP clients that support remote Streamable HTTP and OAuth can connect in the same way.

## Local development

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev
uv run uvicorn steam_companion.app:app --app-dir src --host 127.0.0.1 --port 8000
uv run pytest
```

Build and run the Docker test suite with:

```sh
docker build --target test -t steam-companion:test .
docker run --rm steam-companion:test
```

The app validates required secrets, the single-account allowlist, and the canonical HTTPS origin at startup. SQLite runs through `aiosqlite`; the application is intentionally a single-process SQLite service rather than a multi-user database deployment.

## Known limits

- This release supports exactly one configured Steam account.
- Steam can omit never-launched free-to-play games from the visible owned-games response. Their ownership remains unknown unless another response establishes it.
- Co-op matching checks a bounded set of friends and only uses public, available libraries. Friend SteamIDs are not returned by the tool.
- Achievement data depends on Steam exposing the relevant game schema and player statistics.
- Price history begins when this service first observes a tracked game's price. It is not Steam's all-time price history; no polling runs in the background.
- Package analysis can confirm visible ownership, but cannot infer ownership from absence alone or assign duplicate monetary value when item or currency data is unknown.
- Follows, full catalog discovery, multiple store connections, and a desktop sidecar are not implemented.

## Project layout

```text
src/steam_companion/   providers, application services, MCP registration, and web app
tests/                 service, MCP contract, security, cache, and persistence tests
deploy/                example reverse-proxy configuration
```
