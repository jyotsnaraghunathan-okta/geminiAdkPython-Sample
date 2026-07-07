Google ADK Agent (without adapter) (cloudshell_adk_idjag_sdk.py)
End-to-end guide for the Google Agentspace ADK agent that connects two Okta resource connections for the same AI agent, both starting from the Agentspace access token:
Github repo -  Google ADK Agent using Okta Python SDK (No adapter)	

NOTE:  This ADK Agent sample is a Prototype only.  It stores the agent’s RSA private key in a .env file.  This is not production ready. Ideally, all secrets should be stored in a secrets manager (e.g. **Google Secret Manager**)   and fetched it at runtime, instead of a plaintext `.env` / `env_vars`. 



Smart Triage
Cross-App Access / ID-JAG, via the okta-client-python SDK 
This is a custom MCP server protected by Okta custom AuthZ server.
Github repo
GitHub MCP
Okta STS / brokered consent, a single RFC 8693 token exchange done with manual httpx
3rd party resource protected by 3rd party authorization server (github)
Github Configuration Internal doc  

1. Pre-Read
Initiating ID-JAG flow with an access_token

Google Agentspace passes only the access token—not the ID token—to the ADK agent via tool_context.state. Consequently, any downstream token exchange initiated by the agent (such as Smart Triage ID-JAG or GitHub oauth-sts) must use subject_token_type = urn:ietf:params:oauth:token-type:access_token, differing from typical Okta samples that rely on an ID token. Because the access token is the sole subject token available, the Agentspace authorizer must include a resource parameter during the initial OAuth request. This ensures the minted access token carries the correct audience binding (aud) required for Okta to accept it during the downstream exchange.
2. Architecture
sequenceDiagram
      autonumber
      actor U as End User
      participant AS as Gemini Enterprise / Agentspace
      participant OK as Okta (Custom / Org / Resource AS)
      participant AG as ADK Agent (Vertex AI Agent Engine)
      participant ST as Smart Triage MCP
      participant GH as GitHub MCP

      U->>AS: chat request
      AS->>OK: OAuth login @ Custom AS (resource=…)
      OK-->>AS: id_token + access_token
      Note over AS: Stores ONLY the access_token<br/>(aud = https://smarttriage.com/aud)<br/>⛔ id_token is NOT passed to the agent
      AS->>AG: invoke agent (access_token in session.state)
      Note over AG: _ensure_tokens() — both exchanges use<br/>subject_token_type = access_token

      rect rgb(235,245,255)
      Note over AG,OK: Smart Triage — ID-JAG (okta-client-python SDK)
      AG->>OK: token-exchange: access_token → ID-JAG (Org AS)
      OK-->>AG: ID-JAG
      AG->>OK: jwt-bearer: ID-JAG → resource token (Resource AS)
      OK-->>AG: resource token → _token_store
      end

      rect rgb(235,255,235)
      Note over AG,OK: GitHub — Okta STS / brokered consent (manual httpx)
      AG->>OK: token-exchange: access_token → oauth-sts (Org AS, resource=ORN)
      alt consent already granted
          OK-->>AG: 200 access_token → _gh_token_store
      else interaction_required
          OK-->>AG: 400 interaction_required + interaction_uri
          AG-->>AS: surface "🔐 Authorize GitHub" link
          AS-->>U: click to consent, then retry
      end
      end

      AG->>ST: tools/list + tool calls (Bearer from _token_store)
      ST-->>AG: data
      AG->>GH: tools/list + tool calls (Bearer from _gh_token_store)
      GH-->>AG: data
      AG-->>AS: tool result
      AS-->>U: answer

Why i’m using GitHub uses manual httpx (not the SDK): SDK's TokenExchangeFlow parses an interaction_required response into an OAuth2Error keeping only error/error_description and drops Okta's non-standard interaction_uri (additional_fields came back {}). Even Okta's own notebook sample reads interaction_uri from a side-channel global, not the SDK. Manual httpx reads the full JSON body, so we can surface the sts/redirect consent link. Smart Triage's ID-JAG has no such consent step, so it stays on the SDK.

3: Complete the Admin Prerequisites in Google Cloud Console
Because you are exposing external infrastructure to an enterprise AI environment, Google Cloud enforces strict guardrails. You must complete these two quick admin tasks first:
Override the Org Policy: By default, Google Cloud blocks custom MCP endpoints.
Go to Organization Policies in the GCP Console.
Filter for the policy: Disable custom mcp server connector for gemini enterprise.
Click Manage Policy > Check Override parent's policy.
Add a rule setting the enforcement toggle to OFF, then click Set Policy.
Assign IAM Roles: Ensure your account (and the account creating the data store) has the Discovery Engine Editor (roles/discoveryengine.editor) role assigned.
GCP project with Vertex AI API enabled.
GCP Cloud Storage bucket for ADK staging artifacts.

4: Okta configuration
In this example, we will represent the Gemini Agent as a first class digital identity in Okta. We will enable 2 resource connections for this Agent.
Custom MCP server.
GitHub - 3rd party MCP.

In the Okta admin console, navigate to Directory -> AI Agents
Create a new agent manually.  Call it “Gemini-EnterpriseTools”.
Give it a description.
Add owners (group or individual users). Owners are the people responsible for this agent (not to be confused with the developers of the agent). This will be used for Access Certifications and notifications if there are any issues with the agent execution.
Under delegations, choose - “user sign-on” , since we are showing an interactive user flow. I.e. a user who’s logged into Gemini Enterprise is requesting agents to act on their behalf. 
Setup the non-human identity caller based on this doc - Initiating ID-JAG flow with an access_token

In my example,  I created a new web app called - “Gemini - EnterpriseTools” and assigned this as the caller/initiator of this agent. 
Next, under resource connections, we will connect to 2 resources. 
Custom MCP server.  This MCP server is protected by  the Okta Custom Authorization server. 
Github MCP - https://api.githubcopilot.com/mcp 
Setting up the custom MCP server -  
For your convenience, we’ve provided a custom MCP server built on FastMCP. Clone this repo - “SmartTriage” from Github (https://github.com/jyotsnaraghunathan-okta/SmartTriage ) and host it on a platform like render. 

This MCP server is protected by Okta’s custom authorization server. 
Navigate to Security -> API.  Add a custom authorization server called “Smart Triage”.  Audience :  “api://smarttriage”


Add custom scopes: smarttriage:read and smarttriage:write


Create an access policy.  It should be assigned to the AI agent and the web app that the end users authenticate with to authorize the agent to act on their behalf. 




Create a rule and make sure the custom scopes are included. 

Next, add the custom authorization server created above as a resource connection to this agent. 
Under the agent setting, navigate to “resource connections”.
Choose Add resource connection.
Pick “Custom Authorization Server”

Choose the custom authorization server created above and the scopes.



Next, let’s move on to setting up the GitHub MCP Server. Use the following guide to configure Github.
Next add GitHub as a resource connection to this agent. 
Navigate to the “resource connections” section on the agent configuration. 
Click on “Add resource connection”. Choose “Application” as the connection type. Then connect to “OIN app” and pick github from the dropdown.

You can edit the resource indicator and give it a more simpler name. 

5. Prerequisites
GCP project with Vertex AI/Gemini Agentspace enabled + a GCS staging bucket.
Okta tenant with: the Custom AS (issues the user token with aud), the Smart Triage resource authz server, an AI agent identity (wlp...) + RSA key + Cross-App-Access delegation policy, a GitHub MCP-server resource connection attached to that agent, and an OIDC client for the Agentspace authorizer.
The files cloudshell_adk_idjag_sdk.py (and reset_agent_auth.py / reset_native_agent_auth.sh for forcing re-auth).

6. Deploying the custom ADK agent to Gemini AgentSpace
Upload the ADK agent script to google cloudshell. 
Create a .env file in the same location - .env parameters

Read at runtime via os.getenv() (from .env locally; from env_vars on the worker). The code's built-in defaults are generic, so set GCP_PROJECT / GCP_BUCKET (and the Okta/GitHub values) explicitly.
Secrets (private key) live only in a git-ignored .env. This doc uses placeholders. 

Env var
Example
Purpose
GCP_PROJECT
jo-dev-portal
Vertex AI project
GCP_LOCATION
us-central1
Agent Engine region
GCP_BUCKET
gs://jo-dev-portal-adk-staging
ADK staging bucket
OKTA_DOMAIN
https://acme.okta
Okta org base URL
OKTA_ORG_ISSUER
(optional)
Org-AS issuer for the SDK (default OKTA_DOMAIN)
IDJAG_AUDIENCE
https://acme.okta/oauth2/auszrn0q77tsoa7001d7
Smart Triage resource AS (SDK target)
RESOURCE_AUTHZ_SERVER
auszrn0q77tsoa7001d7
Smart Triage resource authz server id
IDJAG_SCOPES
smarttriage:read
Smart Triage scope
AT_AI_AGENT_ID
wlp10mv0rrvI9zG9M1d8
client-assertion iss/sub (agent identity)
AT_AGENT_PRIVATE_KEY_ID
fa21…c38c
signing key kid
AT_AGENT_PRIVATE_KEY_PEM
(RSA PEM)
signs the client assertion
SMARTTRIAGE_MCP_URL
https://smarttriage-1.onrender.com/mcp
Smart Triage MCP endpoint
GITHUB_MCP_URL
https://api.githubcopilot.com/mcp
GitHub MCP endpoint
GITHUB_RESOURCE_ORN
orn:oktapreview:idp:github
Okta resource ORN for the GitHub STS resource=
AUTH_ID_PREFIX
(optional)
state-key prefix (default okta-authorization_native)

Create .env in Cloud Shell
cd ~/native   # folder containing cloudshell_adk_idjag_sdk.py

cat > .env <<'EOF'
OKTA_DOMAIN=https://acme.okta.com
IDJAG_AUDIENCE=https://acme.okta.com/oauth2/auszrn0q77tsoa7001d7
RESOURCE_AUTHZ_SERVER=auszrn0q77tsoa7001d7
IDJAG_SCOPES=smarttriage:read
AT_AI_AGENT_ID=wlp10mv0rrvI9zG9M1d8
AT_AGENT_PRIVATE_KEY_ID=abcd1234
AT_RESOURCE_URI=https://smarttriage.com/aud
GCP_PROJECT=dev-portal
GCP_LOCATION=us-central1
GCP_BUCKET=gs://dev-portal-adk-staging
AUTH_ID_PREFIX=okta-authorization_native
SMARTTRIAGE_MCP_URL=https://smarttriage-1.onrender.com/mcp
GITHUB_MCP_URL=https://api.githubcopilot.com/mcp
GITHUB_RESOURCE_ORN=orn:oktapreview:idp:github
AT_AGENT_PRIVATE_KEY_PEM="-----BEGIN PRIVATE KEY-----
MIIEugIBADANBgkqhkiG9w0BAQEFAASCBKQwggSgAgEAAoIBAQDEIWU3Lpm1Tv42
oj4dAR6BRETIZdBfJSM=
-----END PRIVATE KEY-----"

EOF




Run python cloudshell_adk_idjag_sdk.py in google cloudshell.


Note the LRO url : Create AgentEngine backing LRO: projects/<<project_id>>/locations/us-central1/reasoningEngines/4329171453472669696/operations/612247518466329804 

Navigate to the Gemini Agentspace -> Deployments to confirm that the ADK has been deployed .



7:  Create an Application in Gemini Enterprise and attach the agent to this application. 
In the google cloud console, navigate to Gemini Enterprise.
Click on the Create App button to create a new application. Give it a new name. Leave other fields as default.

Next, click into the newly created application.
Navigate to the “Agents” menu item in the left navigation.


Click on “Add Agent” to add the ADK agent created above to Gemini Enterprise.


Pick the “Custom agent via Agent Runtime” option. 

Click on Add Authorization.


Add the Custom Authorization Server /token an /authorize endpoints. The client_id and client_secret of the web app the user will authenticate with to authorize the agent to act on their behalf. 
NOTE:  For the /authorize endpoint,  we also need to specify additional parameters  (scope, response_type and resource)

https://acme.okta..com/oauth2/aus10mn2tcfNdnFbh1d8/v1/authorize?response_type=code&scope=openid%20profile%20email%20offline_access&resource=https%3A%2F%2Fsmarttriage.com%2Faud

To ensure the agent functions properly and you can reset sessions efficiently, keep these two operational points in mind:
Match IDs and Names: Ensure the Authorization ID is identical to the Authorization Name in your configuration.
Force User Re-Authorization: Agentspace caches the user's access_token until it expires. If you need to quickly reset an active authorization before that expiry, use a script to delete and re-create the authorization. This clears the cache and immediately forces the end user to authenticate the agent again.


Next, we will configure the agent.  You will need to provide a name, ID and description. 
The Agent runtime reasoning id can be found in the Agentspace/Vertex AI -> Deployments under your agent. 



Copy the resource URL from the Agent Identity section under the Vertex AI-> Deployments -> your agent. 
Paste this into the Agent Runtime reasoning engine field above. 


This links your ADK agent to the Gemini Enterprise application. 
Next, we need to give users permissions to access this agent within the app. 
Navigate to the “User Permissions” section under the agent in the Gemini Enterprise App. 

Click “Add User”. For this demo, I have picked “All Users”. 

8:  Testing the Agent
Navigate to “Overview” section of the app and click “Preview”.

Copy the preview URL in the pop-up.

Login as the test user. Note:  You may need to assign a license to the test user if your Gemini Enterprise free trial has expired. 
Once authenticated, navigate to the “Agents” menu item. If the agent has been correctly assigned to this test user, it should show up here. 


Click on the agent. User will be asked to “Authorize”.  This is basically the OIDC web app that we setup in Okta where the user will need to authenticate to give agent permissions to act on their behalf. 



9. Agentspace authorization
The authorizer performs the login. Agentspace only appends client_id, state, redirect_uri — so bake response_type, scope, and resource into the authorizationUri:

https://acme.okta.com/oauth2/aus10mn2tcfNdnFbh1d8/v1/authorize?response_type=code&scope=openid%20profile%20email%20offline_access&resource=https%3A%2F%2Fsmarttriage.com%2Faud

Create/patch it via the Discovery Engine API (see DEPLOY.md §5 for the exact curls). The authorization id prefix must match AUTH_ID_PREFIX (okta-authorization_native).


10. GitHub STS + brokered consent
On the first GitHub request, the agent runs the STS exchange:

POST {ORG}/oauth2/v1/token
  grant_type=urn:ietf:params:oauth:grant-type:token-exchange
  requested_token_type=urn:okta:params:oauth:token-type:oauth-sts
  subject_token=<Agentspace access token>
  subject_token_type=urn:ietf:params:oauth:token-type:access_token
  client_assertion=<private_key_jwt (PyJWT)>, client_assertion_type=jwt-bearer
  resource=orn:oktapreview:idp:github

200 → GitHub token cached (_gh_token_store), used as Bearer to GITHUB_MCP_URL.
400 interaction_required → the agent captures interaction_uri and the instruction provider appends an "🔐 Authorize GitHub →" directive so the model shows the clickable consent link and waits. After the user authorizes, the next turn's STS returns 200.

Consent UX caveat: the first GitHub ask shows the consent link but no GitHub tools yet — tools/list ran before consent and the tolerant toolset returned []. After authorizing, GitHub tools populate on a fresh chat (discovery re-runs with a token).
11. Verify ([idjag-sdk] logs)
gcloud logging read \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND textPayload:"[idjag-sdk]"' \
  --project=jo-dev-portal --freshness=20m --limit=60 \
  --format='value(timestamp,textPayload)'

Expected sequence:

STEP1/2 (Agentspace) access_token received (with aud=https://smarttriage.com/aud)
SmartTriage STEP3 … start() → STEP3 ok → STEP4 … resume() → STEP4 ok: resource token cached
GitHub STS access_token -> oauth-sts (POST …) → GitHub STS response → either 200+token or interaction_required + interaction_uri.

Or ask the agent dump_state → agentspace_auth block reports smarttriage_token_cached, github_token_cached, github_interaction_uri.

Exchanges log only on a cache miss; a warm worker with valid cached tokens prints nothing. The Regional Access Boundary … Account not found line is a benign gcloud warning.


12. Force re-auth & cleanup
Force per-user re-consent by rotating the authorization id (see DEPLOY.md §8):

./reset_native_agent_auth.sh

List/delete stale reasoning engines:

gcloud ai reasoning-engines list --project=jo-dev-portal --region=us-central1 \
  --format='table(name, displayName, createTime)'
gcloud ai reasoning-engines delete <ENGINE_ID> --project=jo-dev-portal --region=us-central1


13. Troubleshooting
Symptom
Cause
Fix
Smart Triage no delegation policy authorizes this token
subject token's aud isn't https://smarttriage.com/aud, or XAA policy missing
fix authorize-time resource; check the Okta delegation policy
GitHub STS invalid_target: 'resource' is invalid
resource sent as a bare string (SDK) or wrong ORN
use manual httpx (current) sending resource=<ORN>; confirm GITHUB_RESOURCE_ORN
GitHub STS interaction_required, no consent link
SDK dropped interaction_uri
use manual httpx (current) — reads interaction_uri from the body
ValueError: Tool 'list_tools' not found (turn crash)
LLM invented a tool because GitHub tools absent + no consent directive
fixed: consent link now surfaces + instruction forbids inventing tools
GitHub MCP 401 on tools/list
no GitHub token yet (consent pending)
expected pre-consent; tolerant toolset returns []; tools appear post-consent
Deploy 500 INTERNAL
worker build failure (dep conflict) or transient
retry; check build logs; adjust requirements
TypeError: unexpected keyword 'env_vars'
old SDK
bake config as constants instead



14. Notes / production hardening
_token_store and _gh_token_store are shared module-level singletons — fine for single-user prototype testing; use ContextVars for multi-user isolation. (Also why GitHub tools appear only after a fresh turn post-consent.)
Remove the dump_state diagnostic before production.
Store the agent's RSA private key in a secrets manager (e.g. **Google Secret Manager**) and fetch it at runtime, instead of a plaintext `.env` / `env_vars`. Rotate it periodically. (`.env` / `env_vars` is fine for prototyping only.)
GitHub STS uses manual httpx deliberately — do not "upgrade" it to the SDK TokenExchangeFlow unless a future SDK version surfaces interaction_uri.

