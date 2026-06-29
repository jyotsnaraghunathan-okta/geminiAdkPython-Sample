"""
cloudshell_adapter_adk_v2.py — Prototype: tool_context.state-based auth.

Auth model
----------
1. An OAuth authorizer is registered in Agentspace config with AUTH_ID.
2. Before each MCP tool call, before_tool_callback reads
   tool_context.state.get(AUTH_ID) — Agentspace puts the live user
   access token there after the OAuth exchange.
3. The token is stored in a module-level _TokenStore and read by
   DynamicStreamableHTTPConnectionParams on every HTTP call to the adapter.

Note: _TokenStore is a simple shared store (not per-asyncio-task). Sufficient
for single-user prototype testing; replace with ContextVar for production.

Prerequisites
-------------
    pip install google-adk google-cloud-aiplatform[adk,reasoningengine] httpx python-dotenv
"""

import json
import os
import re
import sys
from typing import Any, Optional

from dotenv import load_dotenv
import vertexai
from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.tool_context import ToolContext
from vertexai import agent_engines
from vertexai.agent_engines import AdkApp

load_dotenv()

# ── Google Cloud ───────────────────────────────────────────────────────────────

PROJECT  = os.environ["GCP_PROJECT"]
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
BUCKET   = os.environ["GCP_BUCKET"]

vertexai.init(project=PROJECT, location=LOCATION, staging_bucket=BUCKET)


# ── Configuration ──────────────────────────────────────────────────────────────

ADAPTER_URL      = os.environ["ADAPTER_URL"]
MCP_AGENT_HEADER = os.environ.get("MCP_AGENT_HEADER", "gemini-enterprisetools")

# auth_id registered in Agentspace config (without the "temp:" prefix).
AUTH_ID_PREFIX = "okta-authorization"  # matches any suffix, e.g. okta-authorization-1782243784


# ── Schema sanitizer ──────────────────────────────────────────────────────────
#
# Gemini API requires all `enum` values to be strings (TYPE_STRING). Some MCP
# tool schemas use boolean enums (e.g. `"enum": [true, false]`), which causes
# a 400 INVALID_ARGUMENT. We walk the schema and coerce them to strings.

def _sanitize_json_schema(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (
                [str(v) if not isinstance(v, str) else v for v in val]
                if k == "enum" and isinstance(val, list)
                else _sanitize_json_schema(val)
            )
            for k, val in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize_json_schema(i) for i in obj]
    return obj


def _patch_mcp_tool_schema(tool: Any) -> None:
    """Sanitize a tool's schema in place — tries the known ADK attribute paths."""
    # Path 1: raw MCP tool inputSchema (source of truth before conversion)
    mcp_tool = getattr(tool, "_mcp_tool", None)
    if mcp_tool is not None and isinstance(getattr(mcp_tool, "inputSchema", None), dict):
        mcp_tool.inputSchema = _sanitize_json_schema(mcp_tool.inputSchema)

    # Path 2: already-converted FunctionDeclaration parameters
    for fd_attr in ("_function_declaration", "function_declaration"):
        fd = getattr(tool, fd_attr, None)
        if fd is None:
            continue
        for p_attr in ("parameters", "_parameters"):
            params = getattr(fd, p_attr, None)
            if isinstance(params, dict):
                setattr(fd, p_attr, _sanitize_json_schema(params))
                break


class SanitizingMcpToolset(McpToolset):
    """McpToolset that coerces non-string enum values to strings before Gemini sees them."""

    async def get_tools(self, *args, **kwargs):
        tools = await super().get_tools(*args, **kwargs)
        for tool in tools or []:
            try:
                _patch_mcp_tool_schema(tool)
            except Exception as exc:
                print(
                    f"[v2] schema sanitize failed for {getattr(tool, 'name', '?')}: {exc}",
                    file=sys.stderr, flush=True,
                )
        return tools


# ── Token lookup helper ────────────────────────────────────────────────────────

def _as_dict(state: Any) -> dict:
    """Convert an ADK State object or plain dict to a regular dict.

    ADK's State object supports .get() but not .keys()/.items() in all
    versions. We try several access patterns to extract the underlying data.
    """
    if not state:
        return {}
    if isinstance(state, dict):
        return state
    # Works if State implements __iter__ + __getitem__ (MutableMapping pattern)
    try:
        return dict(state)
    except Exception:
        pass
    # ADK internal storage attribute names across versions
    for attr in ("_data", "_delta", "_value", "_state", "_session_state"):
        raw = getattr(state, attr, None)
        if isinstance(raw, dict):
            return raw
    # Pydantic model
    if hasattr(state, "model_dump"):
        try:
            return state.model_dump()
        except Exception:
            pass
    # Last resort: public vars
    try:
        return {k: v for k, v in vars(state).items() if not k.startswith("_")}
    except Exception:
        return {}


def _find_token(state: Any) -> tuple[Optional[str], Optional[str]]:
    """Return (token, matched_key) for the first state entry whose key starts
    with AUTH_ID_PREFIX. Returns (None, None) if not found."""
    for k, v in _as_dict(state).items():
        if k.startswith(AUTH_ID_PREFIX) and v:
            return v, k
    return None, None


# ── Token store ────────────────────────────────────────────────────────────────
#
# Plain mutable object instead of ContextVar — cloudpickle cannot serialize
# ContextVar (C extension type). _TokenStore implements __reduce__ so
# cloudpickle serializes it as a fresh empty instance; the deployed worker
# re-populates it via _inject_credential on every request.

class _TokenStore:
    def __init__(self):
        self._token = ""

    def get(self) -> str:
        return self._token

    def set(self, value: str) -> None:
        self._token = value

    def __reduce__(self):
        return (self.__class__, ())


_token_store = _TokenStore()


# ── Dynamic connection params ──────────────────────────────────────────────────
#
# __reduce__ on the instance tells cloudpickle to serialize only (url, timeout)
# and reconstruct via _make_dynamic_params. The class is never pickled by value,
# so the property lambda's reference to _token_store is never traversed by
# cloudpickle during serialization.

class DynamicStreamableHTTPConnectionParams(StreamableHTTPConnectionParams):

    def __reduce__(self):
        return (_make_dynamic_params, (self.url, self.timeout))


def _make_dynamic_params(url: str, timeout: float) -> DynamicStreamableHTTPConnectionParams:
    return DynamicStreamableHTTPConnectionParams(url=url, timeout=timeout)


DynamicStreamableHTTPConnectionParams.headers = property(  # type: ignore[assignment]
    lambda self: {
        "X-MCP-Agent": MCP_AGENT_HEADER,
        **({"Authorization": f"Bearer {_token_store.get()}"} if _token_store.get() else {}),
    },
    lambda self, v: None,  # no-op setter
)


# ── Before-tool callback ───────────────────────────────────────────────────────

def _inject_credential(
    tool: Any,
    args: dict,
    tool_context: ToolContext,
) -> Optional[dict]:
    """Read the user's access token from tool_context.state and store in _token_store.

    Agentspace places the token under AUTH_ID after completing the OAuth exchange.
    Returns None so the tool proceeds normally.
    """
    if _token_store.get():
        return None

    access_token, matched_key = _find_token(tool_context.state)
    if access_token:
        _token_store.set(access_token)
        print(f"[v2] token injected from tool_context.state[{matched_key!r}]",
              file=sys.stderr, flush=True)
    else:
        print(f"[v2] no token in tool_context.state matching prefix={AUTH_ID_PREFIX!r}",
              file=sys.stderr, flush=True)

    return None


# ── Session-id sanitization ─────────────────────────────────────────────────────

_FULL_SESSION_RE = re.compile(r"^projects/.+/sessions/([A-Za-z0-9_-]+)$")


def _sanitize_session_ids(obj: Any) -> Any:
    if isinstance(obj, str):
        m = _FULL_SESSION_RE.match(obj)
        return m.group(1) if m else obj
    if isinstance(obj, dict):
        return {k: _sanitize_session_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_session_ids(v) for v in obj]
    return obj


# ── Diagnostic tool ───────────────────────────────────────────────────────────

def dump_state(tool_context: ToolContext) -> dict:
    """Diagnostic: returns all tool_context.state keys to identify the auth_id.

    Ask the agent 'show me your state keys' after deploying to see what key
    Agentspace uses for the OAuth token. Remove this tool once AUTH_ID is confirmed.
    """
    state_dict = _as_dict(tool_context.state)
    state_keys = list(state_dict.keys())
    safe_state = {
        k: ("<token>" if isinstance(v, str) and len(v) > 40 else v)
        for k, v in state_dict.items()
    }
    print(f"[v2][DIAG] state keys: {state_keys}", file=sys.stderr, flush=True)
    print(f"[v2][DIAG] state (redacted): {safe_state}", file=sys.stderr, flush=True)
    return {
        "state_keys": state_keys,
        "state_redacted": safe_state,
        "user_id": getattr(tool_context.session, "user_id", None),
        "session_id": getattr(tool_context.session, "id", None),
    }


# ── Instruction provider ───────────────────────────────────────────────────────
#
# Fires before tools/list (system prompt must be resolved before tool discovery).
# This is the correct interception point for pre-populating _token_store so that
# McpToolset's tools/list call already has the Authorization header.

_BASE_INSTRUCTION = (
    "You are an enterprise AI assistant. Use your tools to help users "
    "with tasks across connected enterprise systems including "
    "Smart Tirage, an Okta-protected MCP server for diagnosing service health, routing incidents, "
    "and tracking cloud spend across a microservices fleet, and GitHub "
    "repository operations. Any questions on GitHub should be directed "
    "to the vsysgithub__ resource exposed by the adapter.\n\n"
    "IMPORTANT: If a tool returns an authorization or consent link "
    "(a URL), do NOT print the raw URL. Render it as a Markdown link "
    "with short call-to-action text so it shows up as a clickable "
    "button, using the EXACT URL from the tool response, e.g.:\n"
    "    **[🔐 Authorize →](PASTE_THE_EXACT_URL_HERE)**\n"
    "Include the service name in the label when the tool provides it "
    "(e.g. '🔐 Authorize GitHub →'). Never alter the URL. Ask the user "
    "to click it to sign in, and do not proceed with other tool calls "
    "until they confirm they have authorized.\n\n"
    "Access boundaries: If a tool returns a 404, error, 'not found', or "
    "'access denied' for a resource, tell the user you do not have access "
    "to it. Never fabricate, guess, or infer its details, and never reuse "
    "data from an earlier response to answer about a resource the current "
    "tool call could not retrieve."
)


def _instruction_provider(context: ReadonlyContext) -> str:
    """Resolve the system prompt — fires before tools/list.

    This is the earliest point where session.state is available. We use it to
    pre-populate _token_store so McpToolset's tools/list call is authenticated.
    """
    state = getattr(context.session, "state", None) or {}
    state_keys = list(state.keys())
    print(f"[v2] instruction_provider session_state keys: {state_keys}",
          file=sys.stderr, flush=True)

    token, matched_key = _find_token(state)
    if token and not _token_store.get():
        _token_store.set(token)
        print(f"[v2] token pre-populated from state[{matched_key!r}]",
              file=sys.stderr, flush=True)
    else:
        print(f"[v2] no token found matching prefix={AUTH_ID_PREFIX!r}, state keys={state_keys}",
              file=sys.stderr, flush=True)

    return _BASE_INSTRUCTION


# ── Agent ──────────────────────────────────────────────────────────────────────

def _build_agent() -> LlmAgent:
    return LlmAgent(
        model="gemini-2.5-flash",
        name="enterprise_adk_agent",
        instruction=_instruction_provider,
        tools=[
            dump_state,
            SanitizingMcpToolset(
                connection_params=DynamicStreamableHTTPConnectionParams(
                    url=ADAPTER_URL,
                    timeout=120,
                ),
            )
        ],
        before_tool_callback=_inject_credential,
    )


# ── Enterprise ADK App ─────────────────────────────────────────────────────────

class EnterpriseAdkApp(AdkApp):
    """AdkApp for Gemini Enterprise.

    Agent is built once at init. Token injection happens per-call via
    before_tool_callback reading tool_context.state[AUTH_ID].
    """

    def __init__(self, **kwargs):
        super().__init__(**(kwargs or {"agent": _build_agent(), "enable_tracing": True}))

    def streaming_agent_run_with_events(self, **kwargs):
        """Sanitize Agentspace session paths, pre-populate token, then delegate to parent.

        We pre-populate _token_store here so McpToolset's tools/list call (which
        happens before before_tool_callback fires) already has the Authorization header.
        Token is read from session state in request_json under AUTH_ID.
        """
        user_id = kwargs.get("user_id", "") or ""
        print(f"[v2] streaming_agent_run_with_events user_id={user_id!r}",
              file=sys.stderr, flush=True)

        rj = kwargs.get("request_json")
        if isinstance(rj, str):
            try:
                parsed = json.loads(rj)
                new_rj = json.dumps(_sanitize_session_ids(parsed))
                if new_rj != rj:
                    print("[v2] stripped Agentspace session path -> bare id",
                          file=sys.stderr, flush=True)
                kwargs["request_json"] = new_rj
            except Exception as exc:
                print(f"[v2] request_json sanitize skipped: {exc}",
                      file=sys.stderr, flush=True)
        elif rj is not None:
            kwargs["request_json"] = _sanitize_session_ids(rj)

        for _skey in ("session_id", "session"):
            if isinstance(kwargs.get(_skey), str):
                kwargs[_skey] = _sanitize_session_ids(kwargs[_skey])

        return super().streaming_agent_run_with_events(**kwargs)


# ── Deploy ─────────────────────────────────────────────────────────────────────

remote_app = agent_engines.AgentEngine.create(
    EnterpriseAdkApp(),
    requirements=[
        "google-adk==1.33.0",
        "google-cloud-aiplatform[adk,reasoningengine]",
        "mcp",
        "httpx",
    ],
    display_name="Jo simplified agent",
)

print("Done! Resource name:", remote_app.resource_name)
print()
print("Update RESOURCE_NAME in test_agent.py to:", remote_app.resource_name)
