# Deploy on Railway

## 1. Create the service

After reviewing and merging the implementation PR, connect the repository to a Railway project. Railway should detect `Dockerfile` and `railway.toml`. Use **one replica**, one process, and turn off sleeping/serverless mode so the queue can run continuously. Enable a spending alert.

Attach a persistent volume at `/data`. Set `DATABASE_PATH=/data/dispatch.sqlite3`. The container entrypoint prepares the mount's ownership, then drops to UID 10001 before starting the HTTP server. Leave Railway's run UID at its default so initialization can handle a root-owned mount. Do not make the database public or world-writable.

Generate a Railway HTTPS domain. Set `BASE_URL` to that exact origin without a trailing path. Configure the health check `/healthz` and route to the injected `PORT`.

## 2. Add private variables

Run `python scripts/configure.py` locally to generate `.env`. Transfer `ADMIN_TOKEN`, `INDEX_TOKEN`, and `ENCRYPTION_KEY` into Railway's variables UI. Do not put credentials in GitHub, a prompt, screenshots, or the public URL. Keep a secure backup of `ENCRYPTION_KEY`; changing it makes existing subscription secrets unreadable.

| Variable | Value |
|---|---|
| `BASE_URL` | Your HTTPS service origin |
| `DATABASE_PATH` | `/data/dispatch.sqlite3` |
| `ADMIN_TOKEN` | Generated secret, dashboard/owner authorization |
| `INDEX_TOKEN` | Different generated secret, Pebble webhook only |
| `ENCRYPTION_KEY` | Generated Fernet key |
| `NOTE_DESTINATIONS` | JSON alias → Notion page ID mapping, e.g. `{"inbox":"PAGE_ID","ideas":"PAGE_ID"}` |
| `DEFAULT_DESTINATION` | `inbox` |
| `NOTION_TOKEN` | Your Notion internal connection token or personal access token |
| `ROUTER_MODE` | `jev` for Jev routing; `rules` for commands only; `openai` as an alternative |
| `TYPESAFE_API_KEY` | Jev API key (`JEV_API_KEY` is also accepted) |
| `JEV_MODEL`, `JEV_MIN_CONFIDENCE` | Optional: `jev-latest`, `0.75` |
| `NOTE_DESTINATION_DESCRIPTIONS` | Optional JSON alias → routing description; only configured aliases |
| `OPENAI_API_KEY`, `ROUTER_MODEL` | Required only for `openai` routing |
| `OAUTH_REDIRECT_URIS` | Exact HTTPS redirect URI(s) shown during your ChatGPT connection setup, comma-separated |
| `DOTS_CALLBACK_HOSTS` | Exact permitted callback hostname(s); starts with `chatgpt.com`, adjust from the actual verified ChatGPT callback |

In the service **Variables → RAW Editor**, add the values from your private `.env`, preserving existing variables. Save and deploy the staged changes. GitHub deployment does not upload your local `.env`. Missing page mappings are allowed at startup: notes remain in review until you configure them, and Dots tasks wait for a subscription.

Railway variables are raw values: enter JSON without wrapping shell quotes. `.env` uses shell quotes because local setup sources it.

## 3. Connect Notion

Use a Notion personal access token with Notion API capability and access to the destination pages, or create an internal connection with permission to insert content and explicitly grant it access to each target page. Configure ordinary page IDs, not database/data-source IDs. The adapter appends paragraphs to pages. Your ChatGPT Notion connection does not automatically grant this hosted server a reusable Notion token.

Open the dashboard, authenticate, submit `Note to inbox: Index Dispatch setup test`, refresh and confirm `completed`. Check the actual page. If the result is uncertain, look for the request ID before retrying.

## 4. Connect Dots

Create a private/custom plugin or remote MCP connection in your ChatGPT workspace pointing to `https://YOUR_DOMAIN/mcp` with OAuth. The server exposes OAuth discovery and dynamic public-client registration. Copy the exact redirect URL from the setup flow into `OAUTH_REDIRECT_URIS`, then reconnect if necessary. Approve on your hosted Index Dispatch authorization page using `ADMIN_TOKEN`.

MCP Events requires a workspace/account with the feature available. Follow [OpenAI's current connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt) and [MCP Events requirements](https://developers.openai.com/plugins/build/mcp-events). Plugin packaging and workspace permissions may vary; this repository supplies the server, not a published marketplace listing.

Give your Dot [this prompt](docs/dots-setup.md). The Dot must actually subscribe to `task.created`; merely connecting tools does not establish a subscription. Confirm the dashboard shows “Dots connected.” Then submit `Dots, reply with a short confirmation that Index Dispatch is connected` and verify receipt and a final result.

If verification fails, check the actual callback hostname against `DOTS_CALLBACK_HOSTS` and public HTTPS reachability. Do not disable TLS or public-IP checks. A new client or different Dot requires disconnecting the existing subscription first. The dashboard's Disconnect Dots button revokes all connection tokens as well.

## 5. Connect Index 01

In Pebble's Index advanced webhook settings:

- URL: `https://YOUR_DOMAIN/webhooks/index`
- Method: POST; delivery: **text only**.
- Header: `Authorization: Bearer YOUR_INDEX_TOKEN`.
- Enable the desired recording triggers.

The receiver accepts Pebble's multipart fields `transcription`, `recordedAt`, and `client`. Say one note and one Dots task, and check both their dashboard status and their actual destination. Pebble's default agent still runs; avoid configuring it to perform the same downstream write.

## Operations

Enable volume backups. Back up SQLite using its backup API or stop the process for a consistent filesystem copy; copying only the `.sqlite3` file while WAL mode is active can lose changes. Protect the backup along with its transcripts and keys. There is no automatic data-retention cleanup in v1.

Use the dashboard for failed/review requests. Check an uncertain Notion page before selecting “Approve & send.” Do not scale this SQLite deployment horizontally. Move to transactional job claims and a shared database before multi-user or multiple-worker operation.

Release gate: local tests and CI must pass, the PR must be reviewed, and the two live smoke tests above must succeed before treating this as a working personal workflow.
