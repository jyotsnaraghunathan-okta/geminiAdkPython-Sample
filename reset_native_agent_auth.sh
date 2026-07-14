#!/usr/bin/env bash
#
# reset_native_agent_auth.sh — force re-consent for the "Native Agent" (ADK, direct
# ID-JAG) by rotating its OAuth authorization to a fresh id.
#
# Thin wrapper over reset_agent_auth.py with this agent's values hardcoded:
#   agent  : 15255383464339317249  (Native ADKAgent)
#   engine : native-agent
#   auth   : okta-authorization-native-<timestamp>  (Okta AS aus10d6e1cyFD5hSZ1d8)
#   project: 221328126369  (o4aaproject)
#
# Run in Cloud Shell (uses your gcloud login). Any extra flags are passed through
# to reset_agent_auth.py, e.g.:  ./reset_native_agent_auth.sh --client-secret '...'
#
set -euo pipefail

AGENT_ID="15255383464339317249"
AUTH_BASE="okta-authorization-native"
ENGINE="native-agent"
CLIENT_ID="0oa10d5w4znDWzn0R1d8"
# Resource AS — used for both Agentspace auth and SmartTriage ID-JAG exchange
AUTH_URI="https://oktaforai.oktapreview.com/oauth2/aus10d6e1cyFD5hSZ1d8/v1/authorize?response_type=code&scope=openid%20profile%20email%20offline_access&resource=https%3A%2F%2Fvertex-adk-agent.o4aaproject.example.com"
TOKEN_URI="https://oktaforai.oktapreview.com/oauth2/aus10d6e1cyFD5hSZ1d8/v1/token"
PROJECT="221328126369"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "${SCRIPT_DIR}/reset_agent_auth.py" \
  --agent-id  "${AGENT_ID}" \
  --auth-base "${AUTH_BASE}" \
  --engine    "${ENGINE}" \
  --client-id "${CLIENT_ID}" \
  --auth-uri  "${AUTH_URI}" \
  --token-uri "${TOKEN_URI}" \
  --project   "${PROJECT}" \
  "$@"
