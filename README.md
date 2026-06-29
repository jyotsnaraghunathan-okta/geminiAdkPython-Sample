# Gemini ADK Sample — Enterprise Agent with Okta MCP Adapter

A prototype Google ADK agent that connects to an Okta-protected MCP server and deploys to Vertex AI Agent Engine (Reasoning Engine). It demonstrates `tool_context.state`-based OAuth token injection so Agentspace handles the OAuth exchange and the agent automatically picks up the live user token before each tool call.

## Architecture

```
Agentspace UI
     │  OAuth exchange
     ▼
Vertex AI Agent Engine
     │  tool_context.state["okta-authorization-*"] = <access_token>
     ▼
EnterpriseAdkApp  (AdkApp)
     │  before_tool_callback → _inject_credential → _TokenStore
     ▼
SanitizingMcpToolset  ──HTTP──►  Okta MCP Adapter
                                  (Bearer token in Authorization header)
```

**Key design points:**

- `_instruction_provider` fires before `tools/list`, pre-populating `_TokenStore` so even the tool-discovery call is authenticated.
- `before_tool_callback` (`_inject_credential`) refreshes the token on every tool call as a fallback.
- `_TokenStore` uses `__reduce__` so cloudpickle serializes it as an empty instance; the deployed worker re-populates it per request.
- `SanitizingMcpToolset` coerces non-string `enum` values to strings — required because the Gemini API rejects boolean enums in tool schemas.

## Prerequisites

- Python 3.11+
- A Google Cloud project with Vertex AI enabled
- A GCS bucket for ADK staging artifacts
- An Okta-protected MCP adapter URL
- Agentspace configured with an OAuth authorizer whose ID starts with `okta-authorization`

```bash
pip install google-adk google-cloud-aiplatform[adk,reasoningengine] httpx python-dotenv
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `GCP_PROJECT` | Yes | Google Cloud project ID |
| `GCP_BUCKET` | Yes | GCS staging bucket (`gs://...`) |
| `ADAPTER_URL` | Yes | Base URL of the Okta MCP adapter |
| `GCP_LOCATION` | No | Vertex AI region (default: `us-central1`) |
| `MCP_AGENT_HEADER` | No | Value for `X-MCP-Agent` header (default: `gemini-enterprisetools`) |

> `.env` is git-ignored and should never be committed.

## Deploy

Running the script deploys the agent to Vertex AI Agent Engine:

```bash
python cloudshell_adapter_adk_v2.py
```

On success it prints the resource name:

```
Done! Resource name: projects/<project>/locations/<location>/reasoningEngines/<id>
Update RESOURCE_NAME in test_agent.py to: projects/...
```

Copy that resource name into your test script before running queries against the deployed agent.

## Diagnostics

The agent exposes a `dump_state` tool. After deploying, ask the agent:

```
show me your state keys
```

This returns all keys in `tool_context.state` (with long string values redacted) so you can confirm which key Agentspace is using for the OAuth token. Remove `dump_state` from the agent's tool list once `AUTH_ID_PREFIX` is confirmed.

## Notes

- `_TokenStore` is a module-level singleton — safe for single-user prototype testing. For multi-user production use, replace it with a `ContextVar`.
- The `dump_state` diagnostic tool should be removed before production deployment.
