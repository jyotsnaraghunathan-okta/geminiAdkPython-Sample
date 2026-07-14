"""
cloudshell_adk_idjag_sdk.py — ADK agent: Custom MCP (ID-JAG) + GitHub (STS), both via SDK.

Two Okta resource connections for the same AI agent (wlp10mv0rrvI9zG9M1d8), both
driven through Okta's okta-client-python SDK and both starting from the same
Agentspace access token (subject_token_type = access_token):

  1. Custom MCP  — Cross-App Access / ID-JAG, via CrossAppAccessFlow
                     (start() = access_token->ID-JAG, resume() = ID-JAG->resource token).
  2. GitHub MCP    — Okta STS / brokered consent, a single RFC 8693 token exchange
                     (requested_token_type = urn:okta:params:oauth:token-type:oauth-sts),
                     via the SDK's TokenExchangeFlow. On interaction_required the SDK
                     raises an OAuth2Error that DROPS Okta's non-standard interaction_uri
                     (CONFIRMED 2026-07-07, SDK 0.2.0: OAuth2Error has no field for it), so
                     an APIClientListener (did_send) grabs it from the raw response body
                     before exchange() discards it — see _StsInteractionListener.

Each resource gets its own token store and its own MCP toolset (own Bearer header).

GitHub consent (brokered): if the STS exchange signals interaction_required, the
interaction_uri (https://.../v1/sts/redirect?...) is captured by _StsInteractionListener
from the transport-level response; the instruction provider surfaces it as a clickable
"Authorize GitHub" link and waits. Once consent exists the same exchange returns the
GitHub access_token used as Bearer to the GitHub MCP.

Config is read at runtime via _cfg() (from .env locally, env_vars on the worker).

Integration notes (verify on first redeploy)
---------------------------------------------
* Async bridge: the SDK is async but ADK calls our hooks synchronously inside a
  running loop, so SDK coroutines run in a fresh thread (see _run_async).
* PEM: LocalKeyProvider signs the private_key_jwt client assertion for the SDK — now
  for BOTH Custom MCP (ID-JAG) and GitHub (STS), via the shared _org_oauth_client().
* Org-AS issuer/client for the SDK is built by _org_oauth_client() (OKTA_ORG_ISSUER,
  default OKTA_DOMAIN); both the ID-JAG and STS exchanges post to its token endpoint.
* GitHub tools/list tolerates 401 pre-consent (returns no tools until authorized).

Prerequisites
-------------
    pip install google-adk google-cloud-aiplatform[adk,reasoningengine] \
        mcp okta-client-python cryptography python-dotenv
"""

import asyncio
import base64
import contextvars
import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
import vertexai
from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_session_manager import CheckableMcpHttpClientFactory
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.tool_context import ToolContext
from vertexai import agent_engines
from vertexai.agent_engines import AdkApp

# Okta SDK (installed on the deployed worker via requirements; not needed for py_compile).
from okta_client.authfoundation import (
    OAuth2Client,
    OAuth2ClientConfiguration,
    LocalKeyProvider,
)
from okta_client.authfoundation.oauth2.jwt_bearer_claims import JWTBearerClaims
from okta_client.authfoundation.oauth2.client_authorization import ClientAssertionAuthorization
from okta_client.authfoundation.networking import APIClientListener, APIRetry
from okta_client.oauth2auth import (
    CrossAppAccessFlow,
    CrossAppAccessTarget,
    TokenExchangeFlow,
    TokenType,
)

load_dotenv()

# ── Google Cloud ───────────────────────────────────────────────────────────────

PROJECT  = os.getenv("GCP_PROJECT",  "project")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
BUCKET   = os.getenv("GCP_BUCKET",   "gs://project-adk-staging")

vertexai.init(project=PROJECT, location=LOCATION, staging_bucket=BUCKET)

# ── Configuration ──────────────────────────────────────────────────────────────

_CFG_KEYS = (
    "OKTA_DOMAIN",              # e.g. https://acme.okta.com
    "OKTA_ORG_ISSUER",          # optional Org-AS issuer for the SDK (default: OKTA_DOMAIN)
    "IDJAG_AUDIENCE",           # Custom resource AS issuer: {ORG}/oauth2/{RESOURCE_AUTHZ_SERVER}
    "RESOURCE_AUTHZ_SERVER",    # Custom AS resource authz server id
    "IDJAG_SCOPES",             # e.g. mcp:read
    "AT_AI_AGENT_ID",           # iss/sub of the client assertion (agent identity)
    "AT_AGENT_PRIVATE_KEY_ID",  # kid of the signing key
    "AT_AGENT_PRIVATE_KEY_PEM", # RSA private key (PEM) used to sign the client assertion
    "CUSTOM_MCP_URL",      # Custom MCP endpoint
    "GITHUB_MCP_URL",           # GitHub MCP endpoint
    "GITHUB_RESOURCE_ORN",      # Okta resource ORN for the GitHub STS exchange (resource=)
)

DEFAULT_CUSTOM_MCP_URL = os.getenv("CUSTOM_MCP_URL", "https://custom-mcp-server.com/mcp")
DEFAULT_GITHUB_MCP_URL      = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp")

# auth_id prefix registered in Agentspace config; matches any suffix.
AUTH_ID_PREFIX = os.getenv("AUTH_ID_PREFIX", "okta-authorization_native")


def _cfg(key: str, default: str = "") -> str:
    """Read a config value from the environment at call time."""
    return os.getenv(key, default)


def _private_key_pem() -> str:
    """Return the agent's RSA private key, normalizing escaped newlines."""
    return (
        _cfg("AT_AGENT_PRIVATE_KEY_PEM")
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\r", "")
        .strip()
    )


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
    mcp_tool = getattr(tool, "_mcp_tool", None)
    if mcp_tool is not None and isinstance(getattr(mcp_tool, "inputSchema", None), dict):
        mcp_tool.inputSchema = _sanitize_json_schema(mcp_tool.inputSchema)

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
                    f"[idjag-sdk] schema sanitize failed for {getattr(tool, 'name', '?')}: {exc}",
                    file=sys.stderr, flush=True,
                )
        return tools


class TolerantGitHubMcpToolset(SanitizingMcpToolset):
    """GitHub MCP toolset that tolerates a pre-consent 401 on tools/list: returns
    no tools until the user authorizes (so Custom MCP still loads). GitHub tools
    appear on a later session once the user's GitHub store holds a token."""

    async def get_tools(self, *args, **kwargs):
        try:
            return await super().get_tools(*args, **kwargs)
        except Exception as exc:
            print(f"[idjag-sdk] GitHub tools/list unavailable (likely no consent yet): {exc}",
                  file=sys.stderr, flush=True)
            return []


# ── Token lookup helper ────────────────────────────────────────────────────────

def _as_dict(state: Any) -> dict:
    """Convert an ADK State object or plain dict to a regular dict."""
    if not state:
        return {}
    if isinstance(state, dict):
        return state
    try:
        return dict(state)
    except Exception:
        pass
    for attr in ("_data", "_delta", "_value", "_state", "_session_state"):
        raw = getattr(state, attr, None)
        if isinstance(raw, dict):
            return raw
    if hasattr(state, "model_dump"):
        try:
            return state.model_dump()
        except Exception:
            pass
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


def _decode_jwt_noverify(token: str) -> dict:
    """Decode a JWT payload without verifying the signature (diagnostics only)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode())
    except Exception:
        return {}


# ── Trace logging ────────────────────────────────────────────────────────────

_REDACT_KEYS = {
    "client_assertion", "subject_token", "assertion", "client_secret",
    "access_token", "id_token", "refresh_token", "token",
}


def _redact(d: Any) -> Any:
    if not isinstance(d, dict):
        return d
    return {
        k: (f"<{k}: {len(str(v))} chars>" if k in _REDACT_KEYS and v else v)
        for k, v in d.items()
    }


def _log_step(label: str, req: Any = None, status: Any = None, resp: Any = None) -> None:
    """Emit one exchange trace step to stderr (Cloud Logging), redacting secrets."""
    print(f"[idjag-sdk] {label}", file=sys.stderr, flush=True)
    if req is not None:
        print(f"[idjag-sdk]     req : {json.dumps(_redact(req))}", file=sys.stderr, flush=True)
    if status is not None:
        body = _redact(resp) if isinstance(resp, dict) else resp
        try:
            body_str = json.dumps(body) if isinstance(body, dict) else str(body)[:400]
        except Exception:
            body_str = str(body)[:400]
        print(f"[idjag-sdk]     resp[{status}] : {body_str}", file=sys.stderr, flush=True)


# ── Async bridge + key provider + shared Org-AS client ─────────────────────────

def _run_async(coro_factory):
    """Run an async coroutine from sync code that is itself inside a running event
    loop (ADK's), using a fresh thread + asyncio.run so we never call asyncio.run()
    on an already-running loop."""
    box: dict = {}

    def runner():
        try:
            box["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller thread
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


_key_provider_cache: dict = {}


def _key_provider():
    """Build a LocalKeyProvider from the PEM env var (cached). Prefers an in-memory
    loader; falls back to a temp .pem file if only from_pem_file is available."""
    kp = _key_provider_cache.get("kp")
    if kp is not None:
        return kp

    pem = _private_key_pem()
    kid = _cfg("AT_AGENT_PRIVATE_KEY_ID")

    for meth in ("from_pem", "from_pem_string", "from_pem_data"):
        fn = getattr(LocalKeyProvider, meth, None)
        if callable(fn):
            try:
                kp = fn(pem, algorithm="RS256", key_id=kid)
                break
            except Exception:
                kp = None

    if kp is None:
        fd, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        kp = LocalKeyProvider.from_pem_file(path, algorithm="RS256", key_id=kid)

    _key_provider_cache["kp"] = kp
    return kp


def _org_oauth_client():
    """OAuth2Client for the Okta Org AS, authenticated with the agent's
    private_key_jwt client assertion (SDK-signed via key_provider). Shared by the
    Custom MCP ID-JAG flow and the GitHub STS token exchange."""
    okta = _cfg("OKTA_DOMAIN").rstrip("/")
    org_issuer = _cfg("OKTA_ORG_ISSUER", okta)      # verify on redeploy
    org_token_ep = f"{okta}/oauth2/v1/token"
    agent_id = _cfg("AT_AI_AGENT_ID")
    config = OAuth2ClientConfiguration(
        issuer=org_issuer,
        client_authorization=ClientAssertionAuthorization(
            assertion_claims=JWTBearerClaims(
                issuer=agent_id, subject=agent_id,
                audience=org_token_ep, expires_in=300,
            ),
            key_provider=_key_provider(),
        ),
    )
    return OAuth2Client(configuration=config)


# ── Custom MCP: ID-JAG via okta-client-python CrossAppAccessFlow ─────────────

def _exchange_for_resource_token(user_access_token: str) -> tuple[str, int]:
    """STEP3 (start: access_token -> ID-JAG) + STEP4 (resume: ID-JAG -> resource
    token) via the SDK. Returns (resource_token, exp_epoch); ("", 0) on failure."""
    okta = _cfg("OKTA_DOMAIN").rstrip("/")
    if not okta or not user_access_token:
        _log_step("CustomMCP: missing OKTA_DOMAIN or access token; skipping", status="skip")
        return "", 0

    _sc = _decode_jwt_noverify(user_access_token)
    _log_step("STEP1/2 (Agentspace) access_token received", status="recv",
              resp={"iss": _sc.get("iss"), "aud": _sc.get("aud"), "cid": _sc.get("cid"),
                    "scp": _sc.get("scp"), "exp": _sc.get("exp"), "sub": _sc.get("sub")})

    resource_iss = _cfg("IDJAG_AUDIENCE")             # resource AS issuer
    scopes       = _cfg("IDJAG_SCOPES").split()

    async def _flow():
        flow = CrossAppAccessFlow(client=_org_oauth_client(),
                                  target=CrossAppAccessTarget(issuer=resource_iss))
        _log_step("CustomMCP STEP3 access_token -> ID-JAG via CrossAppAccessFlow.start()",
                  req={"audience": resource_iss, "scope": scopes})
        result = await flow.start(token=user_access_token, token_type="access_token",
                                  audience=resource_iss, scope=scopes)
        _log_step("CustomMCP STEP3 ok: ID-JAG obtained", status="ok",
                  resp={"automatic": getattr(result, "resume_assertion_claims", None) is None})
        _log_step("CustomMCP STEP4 ID-JAG -> resource token via CrossAppAccessFlow.resume()")
        return await flow.resume()

    try:
        token = _run_async(_flow)
    except Exception as exc:
        _log_step("CustomMCP ID-JAG exchange failed", status="ERR", resp=repr(exc))
        return "", 0

    resource_token = getattr(token, "access_token", "") or ""
    if not resource_token:
        _log_step("CustomMCP exchange returned no access_token", status="ERR", resp=repr(token))
        return "", 0

    exp = _resolve_exp(resource_token, getattr(token, "expires_in", None))
    _log_step(f"CustomMCP STEP4 ok: resource token cached (exp={exp}) -> Bearer to MCP")
    return resource_token, exp


def _resolve_exp(token: str, expires_in: Any) -> int:
    """Best-effort resource-token expiry (epoch seconds) for caching."""
    exp = _decode_jwt_noverify(token).get("exp")
    if isinstance(exp, int):
        return exp
    if isinstance(expires_in, int):
        return int(time.time()) + expires_in
    return int(time.time()) + 3600


# ── GitHub: Okta STS / brokered consent (SDK TokenExchangeFlow + listener) ─────
#
# Uses the SDK's TokenExchangeFlow (same client as Custom MCP's ID-JAG). One
# wrinkle: on interaction_required, OAuth2Client.exchange() collapses the response
# body into an OAuth2Error that keeps only error/error_description/error_uri and
# DROPS Okta's non-standard `interaction_uri` (CONFIRMED SDK 0.2.0 — OAuth2Error has
# no field for it). But the transport (DefaultNetworkInterface) returns the 4xx body
# as a RawResponse WITHOUT raising, so APIClient.send() parses the full JSON and
# fires the listener's did_send() with it *before* exchange() raises. An
# APIClientListener therefore recovers interaction_uri from response.result — no
# manual httpx needed.

_OAUTH_STS_TOKEN_TYPE = "urn:okta:params:oauth:token-type:oauth-sts"


class _StsInteractionListener(APIClientListener):
    """Captures Okta's `interaction_uri` from the STS token-exchange response body
    at the transport layer (did_send), before the SDK collapses the body into an
    OAuth2Error that drops it. One instance per exchange (fresh client per call)."""

    def __init__(self) -> None:
        self.interaction_uri = ""

    def will_send(self, client, request) -> None:
        pass

    def did_send(self, client, request, response) -> None:
        result = getattr(response, "result", None)
        uri = (
            result.get("interaction_uri") if isinstance(result, dict)
            else getattr(result, "interaction_uri", None)
        )
        if uri:
            self.interaction_uri = uri

    def did_send_error(self, client, request, error) -> None:
        pass

    def should_retry(self, client, request, rate_limit) -> APIRetry:
        return APIRetry.default()


def _exchange_github_sts(user_access_token: str) -> tuple[str, int, str]:
    """Single Okta STS token exchange for the GitHub MCP resource, via the SDK's
    TokenExchangeFlow (requested_token_type = oauth-sts, resource = GITHUB_RESOURCE_ORN).

    Returns (access_token, exp_epoch, interaction_uri):
      * success + access_token    -> (token, exp, "")            consent already granted
      * interaction_required      -> ("", 0, interaction_uri)    user must consent
      * anything else / error     -> ("", 0, "")
    """
    okta = _cfg("OKTA_DOMAIN").rstrip("/")
    orn  = _cfg("GITHUB_RESOURCE_ORN")
    if not okta or not user_access_token or not orn:
        _log_step("GitHub STS: missing OKTA_DOMAIN / access token / GITHUB_RESOURCE_ORN",
                  status="skip")
        return "", 0, ""

    listener = _StsInteractionListener()
    _log_step("GitHub STS access_token -> oauth-sts via TokenExchangeFlow.start() (SDK)",
              req={"resource": orn, "requested_token_type": _OAUTH_STS_TOKEN_TYPE})

    async def _flow():
        client = _org_oauth_client()
        client.listeners.add(listener)   # capture interaction_uri before exchange() drops it
        flow = TokenExchangeFlow(client=client)
        return await flow.start(
            subject_token=user_access_token,
            subject_token_type=TokenType.ACCESS_TOKEN,
            resource=[orn],
            requested_token_type=_OAUTH_STS_TOKEN_TYPE,
        )

    try:
        token = _run_async(_flow)
    except Exception as exc:
        # interaction_required (brokered consent) surfaces here as OAuth2Error; the
        # listener already grabbed Okta's interaction_uri from the raw 4xx body.
        if listener.interaction_uri:
            _log_step("GitHub STS interaction_required (brokered consent)", status="consent",
                      resp={"interaction_uri": listener.interaction_uri})
            return "", 0, listener.interaction_uri
        _log_step("GitHub STS exchange failed", status="ERR", resp=repr(exc))
        return "", 0, ""

    access_token = getattr(token, "access_token", "") or ""
    if not access_token:
        _log_step("GitHub STS returned no access_token", status="ERR", resp=repr(token))
        return "", 0, ""

    exp = _resolve_exp(access_token, getattr(token, "expires_in", None))
    _log_step(f"GitHub STS ok: token cached (exp={exp}) -> Bearer to GitHub MCP")
    return access_token, exp, ""


# ── Per-user token stores + per-request injection ──────────────────────────────
#
# Token stores are keyed by subject (the Agentspace end-user's session user_id) so a
# warm worker never serves one user's — or a stale — token to another, and a
# not-yet-consented user actually triggers the GitHub consent exchange instead of
# inheriting a cached one. The connection-params `headers` property has no request
# context, so the current subject is carried in a ContextVar set at the request
# entrypoint and in the instruction / before-tool hooks, and read when injecting the
# Bearer. _TokenStore.__reduce__ serializes empty; the worker re-populates per request.

class _TokenStore:
    def __init__(self):
        self._token = ""
        self._exp = 0

    def get(self) -> str:
        return self._token

    def set(self, value: str, exp: int) -> None:
        self._token = value
        self._exp = exp

    def valid(self) -> bool:
        return bool(self._token) and int(time.time()) < (self._exp - 60)

    def __reduce__(self):
        return (self.__class__, ())


_token_stores: dict = {}       # Custom MCP resource tokens, keyed by subject
_gh_token_stores: dict = {}    # GitHub STS tokens, keyed by subject
_gh_states: dict = {}          # per subject: {"interaction_uri": <last GitHub consent URL>}
_auth_diags: dict = {}         # dump_state diagnostics, keyed by subject
_last_subject: dict = {"value": ""}  # most-recent identified subject; header fallback for when the
                                     # ContextVar doesn't reach the MCP header read (see _bearer)

# Current request's subject, read by the connection-params `headers` property (which
# has no request context). Set at the request entrypoint and the instruction /
# before-tool hooks, all of which run within the request's task/context.
#
# The ContextVar is created LAZILY on the worker (same pattern as _key_provider_cache):
# a module-level ContextVar gets captured by cloudpickle when the agent is serialized
# for deployment and fails with "cannot pickle ContextVar". This cache dict is empty at
# pickle time, so it serializes fine, and the worker builds the ContextVar on first use.
_ctx_cache: dict = {}


def _subj_var() -> "contextvars.ContextVar":
    v = _ctx_cache.get("subject")
    if v is None:
        v = contextvars.ContextVar("idjag_subject", default="")
        _ctx_cache["subject"] = v
    return v


def _new_diag() -> dict:
    return {
        "subject": "",
        "agentspace_token_received": False,
        "matched_key": None,
        "subject_claims": {},
        "custommcp_token_cached": False,
        "github_token_cached": False,
        "github_interaction_required": False,
        "github_interaction_uri": "",
    }


def _store_for(stores: dict, subject: str) -> "_TokenStore":
    st = stores.get(subject)
    if st is None:
        st = _TokenStore()
        stores[subject] = st
    return st


def _diag_for(subject: str) -> dict:
    d = _auth_diags.get(subject)
    if d is None:
        d = _new_diag()
        _auth_diags[subject] = d
    return d


def _gh_state_for(subject: str) -> dict:
    s = _gh_states.get(subject)
    if s is None:
        s = {"interaction_uri": ""}
        _gh_states[subject] = s
    return s


def _subject_key(session: Any, access_token: str = "") -> str:
    """Stable per-user key. Prefers the session user_id (present in both the
    instruction context and the tool context); falls back to the access token's
    sub/uid claim when the session has none. Returns "" when unidentifiable."""
    uid = str(getattr(session, "user_id", "") or "")
    if uid:
        return uid
    claims = _decode_jwt_noverify(access_token) if access_token else {}
    return str(claims.get("sub") or claims.get("uid") or "")


def _bearer(stores: dict, label: str = "MCP") -> dict:
    """Authorization header for the current request's subject. Resolves the subject
    from the ContextVar; if that hasn't propagated to this MCP header read (ADK may run
    tools/list in a different context than the hook that set it), falls back to the
    most-recently-identified subject so the just-active user's token is still injected.
    Logs a miss so the deploy logs reveal whether the ContextVar actually propagated."""
    cv = _subj_var().get()
    subject = cv or _last_subject["value"]
    store = stores.get(subject)
    token = store.get() if store else ""
    if not token:
        print(f"[idjag-sdk] {label} bearer: NO token "
              f"(cv={cv!r} fallback={_last_subject['value']!r} known={list(stores.keys())})",
              file=sys.stderr, flush=True)
    else:
        print(f"[idjag-sdk] {label} bearer: token injected "
              f"(subject={subject!r}, via={'cv' if cv else 'fallback'})",
              file=sys.stderr, flush=True)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _ensure_resource_token(state: Any, subject: str) -> None:
    """Custom MCP ID-JAG exchange (SDK) → subject's store, when empty/expired."""
    store = _store_for(_token_stores, subject)
    if store.valid():
        return
    access_token, _ = _find_token(state)
    if not access_token:
        return
    token, exp = _exchange_for_resource_token(access_token)
    if token:
        store.set(token, exp)
        _diag_for(subject)["custommcp_token_cached"] = True


def _ensure_github_token(state: Any, subject: str) -> None:
    """GitHub STS exchange (SDK) → subject's store, or capture the consent
    interaction_uri when brokered consent is required."""
    store = _store_for(_gh_token_stores, subject)
    if store.valid():
        return
    access_token, _ = _find_token(state)
    if not access_token:
        return
    token, exp, interaction_uri = _exchange_github_sts(access_token)
    gh_state = _gh_state_for(subject)
    diag = _diag_for(subject)
    if token:
        store.set(token, exp)
        gh_state["interaction_uri"] = ""
        diag.update(github_token_cached=True,
                    github_interaction_required=False, github_interaction_uri="")
    elif interaction_uri:
        gh_state["interaction_uri"] = interaction_uri
        diag.update(github_token_cached=False,
                    github_interaction_required=True, github_interaction_uri=interaction_uri)


def _ensure_tokens(state: Any, subject: str) -> None:
    """Record the incoming Agentspace token for `subject`, then run both resource
    exchanges into that subject's stores. Failures in one resource never block the
    other. `subject` is the per-user key (see _subject_key)."""
    diag = _diag_for(subject)
    access_token, matched_key = _find_token(state)
    if not access_token:
        # Don't clobber diag for a subject whose tokens are already established: the
        # Agentspace token appears in session.state at instruction time but not in
        # tool_context.state at tool-call time, so this path legitimately sees none.
        st = _token_stores.get(subject)
        gh = _gh_token_stores.get(subject)
        if not ((st and st.valid()) or (gh and gh.valid())):
            diag.update(agentspace_token_received=False, matched_key=None, subject_claims={})
        print(f"[idjag-sdk] no access token in state for subject={subject!r} "
              f"matching prefix={AUTH_ID_PREFIX!r}", file=sys.stderr, flush=True)
        return

    if subject:
        _last_subject["value"] = subject   # header fallback when the ContextVar doesn't propagate

    _claims = _decode_jwt_noverify(access_token)
    diag.update(
        subject=subject,
        agentspace_token_received=True,
        matched_key=matched_key,
        subject_claims={k: _claims.get(k) for k in ("iss", "aud", "exp", "cid", "scp")},
    )

    for name, fn in (("CustomMCP", _ensure_resource_token), ("GitHub", _ensure_github_token)):
        try:
            fn(state, subject)
        except Exception as exc:
            print(f"[idjag-sdk] {name} token ensure failed (subject={subject!r}): {exc}",
                  file=sys.stderr, flush=True)


# ── Dynamic connection params (one per resource, own Bearer header) ────────────

class DynamicStreamableHTTPConnectionParams(StreamableHTTPConnectionParams):
    """Custom MCP connection params — Bearer from the current subject's store."""

    def __reduce__(self):
        return (_make_st_params, (self.url, self.timeout))


def _make_st_params(url: str, timeout: float) -> "DynamicStreamableHTTPConnectionParams":
    return DynamicStreamableHTTPConnectionParams(url=url, timeout=timeout)


DynamicStreamableHTTPConnectionParams.headers = property(  # type: ignore[assignment]
    lambda self: _bearer(_token_stores, "CustomMCP"),
    lambda self, v: None,
)


class _HangingSSEStream(httpx.AsyncByteStream):
    """Fake persistent SSE stream — sends keepalive comments every 60 s so the
    MCP TaskGroup's GET task stays alive while tool calls proceed via POST."""

    async def __aiter__(self):
        try:
            while True:
                await asyncio.sleep(60)
                yield b": keepalive\n\n"
        except asyncio.CancelledError:
            return


class _GitHub405SilentTransport(httpx.AsyncBaseTransport):
    """Converts 405 GET (api.githubcopilot.com/mcp has no SSE support) to a
    fake persistent SSE stream so the MCP TaskGroup doesn't raise and POST-based
    tool calls can proceed normally."""

    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.method == "GET" and response.status_code == 405:
            await response.aclose()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                stream=_HangingSSEStream(),
                request=request,
            )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class _GitHubHttpClientFactory:
    """CheckableMcpHttpClientFactory that silently ignores 405 on SSE GET."""

    def __call__(
        self,
        headers: dict[str, str] | None = None,
        timeout=None,
        auth=None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_GitHub405SilentTransport(),
            headers=headers or {},
            timeout=timeout,
            auth=auth,
        )


class GitHubStreamableHTTPConnectionParams(StreamableHTTPConnectionParams):
    """GitHub MCP params — Bearer from the current subject's store."""

    def __reduce__(self):
        return (_make_github_params, (self.url, self.timeout))


def _make_github_params(url: str, timeout: float) -> "GitHubStreamableHTTPConnectionParams":
    return GitHubStreamableHTTPConnectionParams(
        url=url, timeout=timeout, sse_read_timeout=300, terminate_on_close=False,
        httpx_client_factory=_GitHubHttpClientFactory(),
    )


GitHubStreamableHTTPConnectionParams.headers = property(  # type: ignore[assignment]
    lambda self: _bearer(_gh_token_stores, "GitHub"),
    lambda self, v: None,
)


# ── Before-tool callback ───────────────────────────────────────────────────────

def _inject_credential(
    tool: Any,
    args: dict,
    tool_context: ToolContext,
) -> Optional[dict]:
    """Refresh both resource tokens for the current user before each tool call, and
    pin the subject in the ContextVar so the MCP Bearer header resolves to this user's
    store. Returns None so the tool proceeds (GitHub consent is surfaced via the
    instruction provider)."""
    state = tool_context.state
    access_token, _ = _find_token(state)
    subject = _subject_key(getattr(tool_context, "session", None), access_token or "")
    if subject:
        _subj_var().set(subject)   # don't clobber a good value with "" (unidentified)
    _ensure_tokens(state, subject)
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
    """Diagnostic: confirms Agentspace token delivery and both resource exchanges
    (via agentspace_auth, recorded by the instruction provider). Remove before prod."""
    state_dict = _as_dict(tool_context.state)
    state_keys = list(state_dict.keys())
    safe_state = {
        k: ("<token>" if isinstance(v, str) and len(v) > 40 else v)
        for k, v in state_dict.items()
    }

    subject = _subj_var().get() or _subject_key(getattr(tool_context, "session", None))
    diag = _auth_diags.get(subject, _new_diag())
    st_store = _token_stores.get(subject)
    gh_store = _gh_token_stores.get(subject)
    gh_state = _gh_states.get(subject, {})

    print(f"[idjag-sdk][DIAG] subject={subject!r} tool_context.state keys: {state_keys}",
          file=sys.stderr, flush=True)
    print(f"[idjag-sdk][DIAG] agentspace_auth: {diag}", file=sys.stderr, flush=True)
    return {
        "current_subject": subject,
        "known_subjects": list(_auth_diags.keys()),
        "state_keys": state_keys,
        "state_redacted": safe_state,
        "agentspace_auth": diag,
        "custommcp_token_cached": bool(st_store and st_store.valid()),
        "github_token_cached": bool(gh_store and gh_store.valid()),
        "github_interaction_uri": gh_state.get("interaction_uri", ""),
        "user_id": getattr(tool_context.session, "user_id", None),
        "session_id": getattr(tool_context.session, "id", None),
    }


def clear_github_token(tool_context: ToolContext) -> dict:
    """Clear the cached GitHub token for the current user, forcing re-consent on
    the next GitHub tool call. Useful for demos or after revoking GitHub access."""
    subject = _subj_var().get() or _subject_key(getattr(tool_context, "session", None))
    store = _gh_token_stores.get(subject)
    gh_state = _gh_states.get(subject, {})
    if store:
        store.set("", 0)
    gh_state["interaction_uri"] = ""
    diag = _diag_for(subject)
    diag.update(github_token_cached=False, github_interaction_required=False,
                github_interaction_uri="")
    print(f"[idjag-sdk] clear_github_token: cleared for subject={subject!r}",
          file=sys.stderr, flush=True)
    return {"cleared": True, "subject": subject,
            "message": "GitHub token cleared. The next GitHub request will require re-authorization."}


# ── Instruction provider ───────────────────────────────────────────────────────

_BASE_INSTRUCTION = (
    "You are an enterprise AI assistant. Use your tools to help users with two "
    "connected systems: Custom MCP (an Okta-protected MCP server for diagnosing "
    "service health, routing incidents, and tracking cloud spend across a "
    "microservices fleet) and GitHub (via the GitHub MCP server).\n\n"
    "IMPORTANT: If a tool returns an authorization or consent link (a URL), do "
    "NOT print the raw URL. Render it as a Markdown link with short "
    "call-to-action text so it shows up as a clickable button, using the EXACT "
    "URL from the tool response, e.g.:\n"
    "    **[🔐 Authorize →](PASTE_THE_EXACT_URL_HERE)**\n"
    "Include the service name in the label when the tool provides it. Never "
    "alter the URL. Ask the user to click it to sign in, and do not proceed "
    "with other tool calls until they confirm they have authorized.\n\n"
    "Access boundaries: If a tool returns a 404, error, 'not found', or "
    "'access denied' for a resource, tell the user you do not have access to "
    "it. Never fabricate, guess, or infer its details, and never reuse data "
    "from an earlier response to answer about a resource the current tool call "
    "could not retrieve.\n\n"
    "Only call tools that are actually available to you. Never invent or guess a "
    "tool name. If the user asks for something no available tool can do (e.g. a "
    "GitHub action before GitHub is authorized), do not call a tool — instead tell "
    "the user plainly, and if a GitHub authorization link is provided below, present "
    "that clickable link and ask them to authorize first."
)

_GITHUB_CONSENT_TEMPLATE = (
    "\n\nGITHUB AUTHORIZATION REQUIRED: GitHub access has not been consented yet. "
    "If the user asks anything that needs GitHub, present this EXACT URL as a "
    "clickable Markdown button labelled '🔐 Authorize GitHub →' and ask them to "
    "click it to grant access, then stop and wait until they confirm. Do not alter "
    "the URL:\n    {uri}"
)


def _instruction_provider(context: ReadonlyContext) -> str:
    """Resolve the system prompt — fires before tools/list; runs both resource
    exchanges so the MCP tools/list calls are authenticated. If GitHub needs
    brokered consent, append the interaction_uri as an authorize directive."""
    state = getattr(context.session, "state", None) or {}
    try:
        state_keys = list(state.keys())
    except Exception:
        state_keys = list(_as_dict(state).keys())

    access_token, _ = _find_token(state)
    subject = _subject_key(context.session, access_token or "")
    if subject:
        _subj_var().set(subject)
    print(f"[idjag-sdk] instruction_provider subject={subject!r} session_state keys: {state_keys}",
          file=sys.stderr, flush=True)

    _ensure_tokens(state, subject)

    instruction = _BASE_INSTRUCTION
    consent_uri = _gh_states.get(subject, {}).get("interaction_uri", "")
    gh_store = _gh_token_stores.get(subject)
    if consent_uri and not (gh_store and gh_store.valid()):
        instruction += _GITHUB_CONSENT_TEMPLATE.format(uri=consent_uri)
    return instruction


# ── Agent ──────────────────────────────────────────────────────────────────────

def _build_agent() -> LlmAgent:
    return LlmAgent(
        model="gemini-2.5-flash",
        name="Jo_ADKNative",
        instruction=_instruction_provider,
        tools=[
            dump_state,
            clear_github_token,
            SanitizingMcpToolset(
                connection_params=DynamicStreamableHTTPConnectionParams(
                    url=_cfg("CUSTOM_MCP_URL", DEFAULT_CUSTOM_MCP_URL),
                    timeout=120,
                ),
            ),
            TolerantGitHubMcpToolset(
                connection_params=GitHubStreamableHTTPConnectionParams(
                    url=_cfg("GITHUB_MCP_URL", DEFAULT_GITHUB_MCP_URL),
                    timeout=120,
                    sse_read_timeout=0,
                    terminate_on_close=False,
                    httpx_client_factory=_GitHubHttpClientFactory(),
                ),
            ),
        ],
        before_tool_callback=_inject_credential,
    )


# ── Enterprise ADK App ─────────────────────────────────────────────────────────

class EnterpriseAdkApp(AdkApp):
    """AdkApp for Gemini Enterprise. Per-call resource tokens derived from the
    user access token in session.state (Custom MCP = ID-JAG, GitHub = STS)."""

    def __init__(self, **kwargs):
        super().__init__(**(kwargs or {"agent": _build_agent(), "enable_tracing": True}))

    def streaming_agent_run_with_events(self, **kwargs):
        """Pin the request's subject in the ContextVar (so the MCP Bearer resolves to
        this user's store), sanitize Agentspace session paths, then delegate to parent."""
        user_id = kwargs.get("user_id", "") or ""
        if user_id:
            _subj_var().set(user_id)   # set at the entrypoint; child tasks inherit it
        print(f"[idjag-sdk] streaming_agent_run_with_events user_id={user_id!r}",
              file=sys.stderr, flush=True)

        rj = kwargs.get("request_json")
        if isinstance(rj, str):
            try:
                parsed = json.loads(rj)
                new_rj = json.dumps(_sanitize_session_ids(parsed))
                if new_rj != rj:
                    print("[idjag-sdk] stripped Agentspace session path -> bare id",
                          file=sys.stderr, flush=True)
                kwargs["request_json"] = new_rj
            except Exception as exc:
                print(f"[idjag-sdk] request_json sanitize skipped: {exc}",
                      file=sys.stderr, flush=True)
        elif rj is not None:
            kwargs["request_json"] = _sanitize_session_ids(rj)

        for _skey in ("session_id", "session"):
            if isinstance(kwargs.get(_skey), str):
                kwargs[_skey] = _sanitize_session_ids(kwargs[_skey])

        return super().streaming_agent_run_with_events(**kwargs)


# ── Deploy ─────────────────────────────────────────────────────────────────────

def _build_env_vars() -> dict:
    """Copy config from the local environment into a dict passed to the deployed
    worker so os.getenv() resolves it there."""
    env = {}
    for k in _CFG_KEYS:
        v = os.getenv(k)
        if v:
            env[k] = v
    return env


if __name__ == "__main__":
    remote_app = agent_engines.AgentEngine.create(
        EnterpriseAdkApp(),
        requirements=[
            "google-adk==1.33.0",
            "google-cloud-aiplatform[adk,reasoningengine]",
            "mcp",
            "okta-client-python",
            "cryptography",
        ],
        env_vars=_build_env_vars(),
        display_name="Jo_ADKNative",
    )

    print("Done! Resource name:", remote_app.resource_name)
    print()
    print("Update RESOURCE_NAME in test_agent.py to:", remote_app.resource_name)
