#!/usr/bin/env bash
# Fast code redeploy for the AP Copilot invoice-processing agent.
# Rebuilds the container and routes traffic — nothing else.
# Run from the repo root: bash deployment/deploy.sh
#
# First-time / infrastructure setup: use deploy-infra.sh instead.
# Usage: GCP_PROJECT_ID=your-project bash deployment/deploy.sh

set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your GCP project id}"
REGION="us-west1"
SERVICE="invio-invoice-processing-agent-demo"
IMAGE="us-west1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:latest"

# Model defaults — change here to update the service env vars on next deploy.
# These are applied as --update-env-vars so they persist across image swaps.
GEMINI_MODEL_SCREENING="${GEMINI_MODEL_SCREENING:-gemini-3.5-flash-lite}"
GEMINI_MODEL_EXTRACTION="${GEMINI_MODEL_EXTRACTION:-gemini-3.8-flash}"
GEMINI_MODEL_DRAFT_EMAIL="${GEMINI_MODEL_DRAFT_EMAIL:-gemini-3.5-flash-lite}"

# Model Armor (app/model_armor.py) is only touched when you set it here, because
# --update-env-vars would otherwise reset a previously-enabled service on every
# routine redeploy. e.g.  MODEL_ARMOR_ENABLED=true MODEL_ARMOR_ENFORCE=false bash deployment/deploy.sh
ARMOR_ENV=""
if [[ -n "${MODEL_ARMOR_ENABLED:-}" ]]; then
  ARMOR_ENV=",MODEL_ARMOR_ENABLED=${MODEL_ARMOR_ENABLED},MODEL_ARMOR_ENFORCE=${MODEL_ARMOR_ENFORCE:-true}"
fi
# Same rule for PII masking (Model Armor de-identified text; needs MODEL_ARMOR_ENABLED):
#   PII_MASKING_ENABLED=true bash deployment/deploy.sh
if [[ -n "${PII_MASKING_ENABLED:-}" ]]; then
  ARMOR_ENV="${ARMOR_ENV},PII_MASKING_ENABLED=${PII_MASKING_ENABLED}"
fi

echo "=== Step 1: Build + push image (Kaniko cached) ==="
gcloud builds submit --config cloudbuild.yaml --project "$PROJECT" .

echo ""
echo "=== Step 2: Deploy image to Cloud Run ==="
# --no-cpu-throttling + --min-instances=1 are required by the AgentMail WebSocket
# listener (a background task that must run between requests). Passing them here
# also upgrades a service that was first deployed with request-based CPU.
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --no-cpu-throttling \
  --min-instances=1 \
  --update-env-vars="GEMINI_MODEL_SCREENING=${GEMINI_MODEL_SCREENING},GEMINI_MODEL_EXTRACTION=${GEMINI_MODEL_EXTRACTION},GEMINI_MODEL_DRAFT_EMAIL=${GEMINI_MODEL_DRAFT_EMAIL}${ARMOR_ENV}"

URL=$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --project="$PROJECT" \
  --format="value(status.url)")

echo ""
echo "==================================================================="
echo "Deploy complete!"
echo "  Service URL : $URL"
echo "==================================================================="
