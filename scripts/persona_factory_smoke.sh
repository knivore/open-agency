#!/usr/bin/env bash
set -euo pipefail

API="${API:-http://localhost:8000}"
USER_ID="${USER_ID:-persona-smoke-user}"
USER_EMAIL="${USER_EMAIL:-persona-smoke@example.com}"
RUN_SUFFIX="${RUN_SUFFIX:-$(date +%s)}"
CLEANUP="${CLEANUP:-0}"

AUTH=(-H "x-agency-user-id: ${USER_ID}" -H "x-agency-user-email: ${USER_EMAIL}")
JSON=(-H "Content-Type: application/json")

RUN_ID=""
PERSONA_ID=""
MEMORY_ID=""
PUBLISH_JSON=""

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

api_post_json() {
  local path="$1"
  local payload="$2"
  curl -fsS -X POST "${API}${path}" "${AUTH[@]}" "${JSON[@]}" -d "$payload"
}

api_patch_json() {
  local path="$1"
  local payload="$2"
  curl -fsS -X PATCH "${API}${path}" "${AUTH[@]}" "${JSON[@]}" -d "$payload"
}

api_get() {
  local path="$1"
  curl -fsS "${API}${path}" "${AUTH[@]}"
}

api_delete() {
  local path="$1"
  curl -fsS -X DELETE "${API}${path}" "${AUTH[@]}" >/dev/null
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

cleanup_smoke_data() {
  if ! is_true "${CLEANUP}"; then
    return
  fi

  echo "Cleaning up smoke data" >&2
  if [[ -n "${PUBLISH_JSON}" ]]; then
    jq -r '.memory_ids[]?' <<<"${PUBLISH_JSON}" | while read -r published_memory_id; do
      if [[ -n "${published_memory_id}" ]]; then
        api_delete "/memories/${published_memory_id}" || true
      fi
    done
  fi
  if [[ -n "${PERSONA_ID}" ]]; then
    api_delete "/persona/${PERSONA_ID}" || true
  fi
  if [[ -n "${MEMORY_ID}" ]]; then
    api_delete "/memories/${MEMORY_ID}" || true
  fi
}

trap cleanup_smoke_data EXIT

require_bin curl
require_bin jq

echo "Syncing smoke user ${USER_ID}"
curl -fsS -X POST "${API}/users/sync" "${JSON[@]}" -d "{
  \"id\": \"${USER_ID}\",
  \"email\": \"${USER_EMAIL}\",
  \"display_name\": \"Persona Smoke User\"
}" >/dev/null

MEMORY_ID="persona-smoke-source-${RUN_SUFFIX}"
PERSONA_NAME="Persona Smoke ${RUN_SUFFIX}"

echo "Creating source memory ${MEMORY_ID}"
api_post_json "/memories" "{
  \"memory\": {
    \"id\": \"${MEMORY_ID}\",
    \"scope\": \"user\",
    \"content\": \"Release SOP requires approval evidence before deployment. If test evidence is missing, escalate to the release owner. Teams must not bypass the change approval record.\",
    \"summary\": \"Release approval source\",
    \"memory_type\": \"archive\",
    \"tags\": [\"persona-source\", \"release\"],
    \"importance\": 80,
    \"metadata\": {
      \"document_id\": \"doc-${MEMORY_ID}\",
      \"filename\": \"release-sop-${RUN_SUFFIX}.md\",
      \"upload_intelligence\": {
        \"source\": \"smoke\",
        \"document_kind\": \"policy_sop\",
        \"confidence\": 0.9,
        \"recommended\": {\"tags\": [\"release\", \"approval\"]}
      }
    }
  }
}" >/dev/null

echo "Distilling persona draft"
DISTILL_JSON="$(api_post_json "/persona-factory/distill" "{
  \"name\": \"${PERSONA_NAME}\",
  \"description\": \"Smoke test persona for Persona Factory lifecycle.\",
  \"source_memory_ids\": [\"${MEMORY_ID}\"],
  \"persona_type\": \"professional\",
  \"capability_mode\": \"persona_plus_expertise\",
  \"consent_status\": \"explicit_consent\",
  \"source_basis\": \"uploaded_private_material\",
  \"sensitivity_level\": \"standard\",
  \"visibility\": \"private\"
}")"
RUN_ID="$(jq -r '.run.id' <<<"${DISTILL_JSON}")"
PERSONA_ID="$(jq -r '.persona.id' <<<"${DISTILL_JSON}")"
SOURCE_KEY="$(api_get "/persona-factory/runs/${RUN_ID}/source-map" | jq -r '.items[0].key')"

echo "Correcting source classification for ${SOURCE_KEY}"
api_patch_json "/persona-factory/runs/${RUN_ID}/sources/${SOURCE_KEY}/classification" '{
  "classification": "workflow",
  "document_kind": "ticket",
  "content_roles": ["workflow"],
  "extraction_targets": ["workflow", "decision_pattern"],
  "memory_layers": ["procedural"],
  "vector_tags": ["release", "manual-flow"],
  "confidence": 0.95,
  "rationale": "Smoke correction before source re-distillation."
}' >/dev/null

echo "Re-distilling corrected source"
api_post_json "/persona-factory/runs/${RUN_ID}/sources/${SOURCE_KEY}/redistill" '{"limit": 250}' >/dev/null

echo "Approving reviewable items"
api_post_json "/persona-factory/runs/${RUN_ID}/items/bulk-review" '{
  "action": "approve",
  "filters": {},
  "limit": 250
}' >/dev/null

echo "Synthesizing, approving, and publishing"
api_post_json "/persona-factory/runs/${RUN_ID}/synthesize-package" '{}' >/dev/null
api_post_json "/persona-factory/runs/${RUN_ID}/approve" "{\"version\": \"1.0.${RUN_SUFFIX}\"}" >/dev/null
PUBLISH_JSON="$(api_post_json "/persona-factory/runs/${RUN_ID}/publish" '{}')"

echo "Inspecting graph context"
GRAPH_CONTEXT_STATUS="unavailable"
GRAPH_NODE_COUNT="0"
if GRAPH_JSON="$(api_get "/persona/${PERSONA_ID}/graph-context?limit=12" 2>/dev/null)"; then
  GRAPH_CONTEXT_STATUS="available"
  GRAPH_NODE_COUNT="$(jq '.graph.nodes | length' <<<"${GRAPH_JSON}")"
fi

jq -n \
  --arg run_id "${RUN_ID}" \
  --arg persona_id "${PERSONA_ID}" \
  --arg status "$(jq -r '.persona.status' <<<"${PUBLISH_JSON}")" \
  --arg agent_id "$(jq -r '.agent.id' <<<"${PUBLISH_JSON}")" \
  --arg graph_context_status "${GRAPH_CONTEXT_STATUS}" \
  --argjson cleanup_requested "$(is_true "${CLEANUP}" && echo true || echo false)" \
  --argjson memory_count "$(jq '.memory_ids | length' <<<"${PUBLISH_JSON}")" \
  --argjson graph_nodes "${GRAPH_NODE_COUNT}" \
  '{
    run_id: $run_id,
    persona_id: $persona_id,
    persona_status: $status,
    published_agent_id: $agent_id,
    published_memory_count: $memory_count,
    graph_context_status: $graph_context_status,
    graph_node_count: $graph_nodes,
    cleanup_requested: $cleanup_requested
  }'
