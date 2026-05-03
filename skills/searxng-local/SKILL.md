---
name: searxng-local
description: Web search through the bundled Railway SearXNG service
version: 1.0.0
metadata:
  hermes:
    requires:
      env:
        - SEARXNG_URL
    primaryEnv: SEARXNG_URL
---

# SearXNG Web Search

You have access to a SearXNG metasearch service deployed in the same Railway
project. It is already running separately from Hermes Agent, so do not install
SearXNG, run Docker, or use localhost for search.

## Base URL

Read the `SEARXNG_URL` environment variable. On Railway this should point to
the SearXNG service over private networking, for example:

```text
http://SearXNG.railway.internal:8080
```

Always include the port. Railway private networking uses HTTP internally, not
HTTPS.

## Search API

Use the JSON search endpoint:

```text
GET ${SEARXNG_URL}/search?q=YOUR_QUERY&format=json
```

Examples:

```bash
curl "${SEARXNG_URL}/search?q=railway+private+networking&format=json"
curl "${SEARXNG_URL}/search?q=python+uvicorn+starlette&format=json&categories=it"
curl "${SEARXNG_URL}/search?q=architecture+diagram&format=json&categories=images"
```

The `format=json` query parameter is required. Without it, SearXNG returns the
HTML web UI.

## Response Shape

The response contains a `results` array. Each item usually includes:

- `title`: page title
- `url`: source URL
- `content`: text snippet
- `engine`: search engine that returned the result
- `category`: result category

## Troubleshooting

- If `SEARXNG_URL` is empty, the Railway service variable was not wired into the Hermes service.
- If requests fail with connection errors, verify the SearXNG service is deployed and has `PORT=8080`.
- If results are empty, broaden the query or remove the category filter.
