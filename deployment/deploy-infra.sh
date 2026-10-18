#!/usr/bin/env bash
# One-time infrastructure setup for the AP Copilot invoice-processing agent.
# Run this ONCE when bootstrapping a new GCP project, or when re-creating infra.
# For routine code updates use deploy.sh instead.
#
# Run from the repo root:
#   GCP_PROJECT_ID=your-project \
#   AGENTMAIL_INBOX_ID=invoices@agentmail.to \
#   AGENTMAIL_API_KEY=am_... \
#   bash deployment/deploy-infra.sh
#
# Prerequisites: gcloud CLI authenticated, billing enabled, an AgentMail account
# with the inbox already created (https://console.agentmail.to).
#
# Intake model: the service opens an OUTBOUND WebSocket to AgentMail and listens
# for new mail, so there is no Pub/Sub topic, push subscription, Cloud Scheduler
# job or public push endpoint. Because that connection is long-lived, Cloud Run
# must keep CPU allocated outside requests (--no-cpu-throttling) and keep one
# instance warm (--min-instances=1). Never scale this service to zero.

set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your GCP project id}"
INBOX_ID="${AGENTMAIL_INBOX_ID:?Set AGENTMAIL_INBOX_ID to the AgentMail inbox address, e.g. invoices@agentmail.to}"
REGION="us-west1"
SERVICE="invio-invoice-processing-agent-demo"
SA_RUN="ipa-run-sa"

# Gemini model overrides — edit here to change models without touching Python.
GEMINI_MODEL_SCREENING="${GEMINI_MODEL_SCREENING:-gemini-3.5-flash-lite}"
GEMINI_MODEL_EXTRACTION="${GEMINI_MODEL_EXTRACTION:-gemini-3.8-flash}"
GEMINI_MODEL_DRAFT_EMAIL="${GEMINI_MODEL_DRAFT_EMAIL:-gemini-3.5-flash-lite}"
# Minutes of recent unlabeled mail to pick up after each (re)connect.
AGENTMAIL_CATCHUP_MINUTES="${AGENTMAIL_CATCHUP_MINUTES:-30}"
# Model Armor screening of untrusted invoice content (app/model_armor.py). Opt in
# with MODEL_ARMOR_ENABLED=true once the template exists in $REGION; start with
# MODEL_ARMOR_ENFORCE=false (inspect-only) to review verdicts before blocking.
MODEL_ARMOR_ENABLED="${MODEL_ARMOR_ENABLED:-false}"
MODEL_ARMOR_ENFORCE="${MODEL_ARMOR_ENFORCE:-true}"
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-invoice_processing_model_armor}"
# Masks PII in the email text the LLMs read, using the de-identified text Model Armor
# returns (needs the template's advanced Sensitive Data Protection setting and
# MODEL_ARMOR_ENABLED). Opt in with PII_MASKING_ENABLED=true.
PII_MASKING_ENABLED="${PII_MASKING_ENABLED:-false}"
SECRET_NAME="agentmail-api-key"

# Grant a project-level role only if it is not already bound. Re-runs (and
# deployers without project IAM-admin rights) then succeed when the grant already
# exists, instead of failing on setIamPolicy.
ensure_project_role() {
  local member="$1" role="$2"
  if gcloud projects get-iam-policy "$PROJECT" \
      --flatten="bindings[].members" \
      --filter="bindings.role=${role} AND bindings.members=${member}" \
      --format="value(bindings.role)" 2>/dev/null | grep -q .; then
    echo "  already granted: ${role} -> ${member}"
  else
    gcloud projects add-iam-policy-binding "$PROJECT" \
      --member="$member" --role="$role" >/dev/null
    echo "  granted: ${role} -> ${member}"
  fi
}

echo "=== Step 1: Enable APIs ==="
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  logging.googleapis.com \
  cloudtrace.googleapis.com \
  firestore.googleapis.com \
  modelarmor.googleapis.com \
  dlp.googleapis.com \
  --project="$PROJECT"

echo "=== Step 1b: Create the Firestore database (AP policies + PO master data) ==="
# Native-mode (default) database. The location can NOT be changed after creation;
# it defaults to the Cloud Run region. Creating it needs roles/datastore.owner (or
# project Owner) — an already-existing database is left untouched.
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-$REGION}"
if gcloud firestore databases describe --database="(default)" --project="$PROJECT" &>/dev/null; then
  echo "  Firestore (default) database already exists"
else
  gcloud firestore databases create \
    --database="(default)" \
    --location="$FIRESTORE_LOCATION" \
    --type=firestore-native \
    --project="$PROJECT"
fi

echo "=== Step 2: Create service account ==="
gcloud iam service-accounts create "$SA_RUN" \
  --display-name="Invoice Agent Runtime SA" \
  --project="$PROJECT" 2>/dev/null || echo "  $SA_RUN already exists"

echo "=== Step 3: Create Secret Manager secret for the AgentMail API key ==="
if [ -z "${AGENTMAIL_API_KEY:-}" ]; then
  echo "  AGENTMAIL_API_KEY not set — skipping secret creation."
  echo "  Create manually: printf %s \"\$KEY\" | gcloud secrets create $SECRET_NAME --data-file=- --project=$PROJECT"
else
  gcloud secrets create "$SECRET_NAME" \
    --replication-policy="automatic" \
    --project="$PROJECT" 2>/dev/null || echo "  Secret already exists, creating new version"
  printf %s "$AGENTMAIL_API_KEY" | gcloud secrets versions add "$SECRET_NAME" \
    --data-file=- \
    --project="$PROJECT"
  echo "  Secret $SECRET_NAME updated."
fi

SECRET_RESOURCE="projects/$PROJECT/secrets/$SECRET_NAME/versions/latest"

echo "=== Step 3b: Grant Cloud Build permissions to the default compute SA ==="
# Newer GCP projects no longer auto-grant Editor to the default compute SA, so
# `gcloud run deploy --source` (which builds via Cloud Build under that SA)
# fails reading its own uploaded source without this.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format="value(projectNumber)")
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
ensure_project_role "serviceAccount:${COMPUTE_SA}" "roles/cloudbuild.builds.builder"

echo "=== Step 4: Deploy to Cloud Run ==="
# --no-traffic is rejected by gcloud when the service doesn't exist yet (first
# deploy on a fresh project), so only pass it when updating an existing service.
if gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" &>/dev/null; then
  NO_TRAFFIC_FLAG="--no-traffic"
else
  NO_TRAFFIC_FLAG=""
fi

# --no-cpu-throttling: the AgentMail WebSocket listener is a background task and
#   needs CPU between requests (instance-based billing).
# --min/max-instances=1: the SSE broadcaster, HITL sessions and the in-flight set
#   all live in process memory, and a single instance means a single listener.
# --timeout=3600 only bounds individual HTTP requests (SSE streams); it does not
#   affect the outbound WebSocket.
gcloud run deploy "$SERVICE" \
  --quiet \
  --source . \
  --region="$REGION" \
  --project="$PROJECT" \
  --service-account="${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" \
  --no-cpu-throttling \
  --min-instances=1 \
  --max-instances=1 \
  --cpu=2 \
  --memory=2Gi \
  --timeout=3600 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=True,INVOICE_TRACE_VERBOSE=false,OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY,AGENTMAIL_INBOX_ID=${INBOX_ID},AGENTMAIL_API_KEY_SECRET=${SECRET_RESOURCE},AGENTMAIL_INTAKE_ENABLED=true,AGENTMAIL_CATCHUP_MINUTES=${AGENTMAIL_CATCHUP_MINUTES},GEMINI_MODEL_SCREENING=${GEMINI_MODEL_SCREENING},GEMINI_MODEL_EXTRACTION=${GEMINI_MODEL_EXTRACTION},GEMINI_MODEL_DRAFT_EMAIL=${GEMINI_MODEL_DRAFT_EMAIL},MODEL_ARMOR_ENABLED=${MODEL_ARMOR_ENABLED},MODEL_ARMOR_ENFORCE=${MODEL_ARMOR_ENFORCE},MODEL_ARMOR_TEMPLATE=${MODEL_ARMOR_TEMPLATE},MODEL_ARMOR_LOCATION=${REGION},PII_MASKING_ENABLED=${PII_MASKING_ENABLED}" \
  $NO_TRAFFIC_FLAG

URL=$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --project="$PROJECT" \
  --format="value(status.url)")
echo "  Service URL: $URL"

echo "=== Step 5: IAM — runtime SA ==="
ensure_project_role "serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" "roles/aiplatform.user"
# Runtime reads the policies + purchase_orders collections (read-only is enough
# for the service; roles/datastore.user also covers the seed script's writes).
ensure_project_role "serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" "roles/datastore.user"
# Lets the runtime call SanitizeUserPrompt / SanitizeModelResponse on the template.
ensure_project_role "serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" "roles/modelarmor.user"
# PII masking uses the Model Armor template's advanced Sensitive Data Protection
# setting (a DLP inspect + de-identify template). Model Armor itself calls DLP, so
# its SERVICE AGENT (not the runtime SA) needs the DLP roles in the project that
# holds the DLP templates.
if [[ "$PII_MASKING_ENABLED" == "true" ]]; then
  MA_AGENT="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-modelarmor.iam.gserviceaccount.com"
  ensure_project_role "$MA_AGENT" "roles/dlp.user"
  ensure_project_role "$MA_AGENT" "roles/dlp.reader"
fi

if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT" &>/dev/null; then
  gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member="serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT"
else
  echo "  Secret $SECRET_NAME doesn't exist yet — skipping accessor binding."
  echo "  Once it's created, run:"
  echo "    gcloud secrets add-iam-policy-binding $SECRET_NAME --member=serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=$PROJECT"
fi

ensure_project_role "serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" "roles/logging.logWriter"
ensure_project_role "serviceAccount:${SA_RUN}@${PROJECT}.iam.gserviceaccount.com" "roles/cloudtrace.agent"

echo "=== Step 6: Route traffic to the new revision ==="
gcloud run services update-traffic "$SERVICE" \
  --region="$REGION" --project="$PROJECT" \
  --to-latest

echo ""
echo "==================================================================="
echo "Infrastructure setup complete!"
echo "  Service URL   : $URL"
echo "  Open the UI   : $URL"
echo "  Intake status : $URL/intake/status   (connected should be true)"
echo "  Intake inbox  : $INBOX_ID"
echo "  Seed Firestore (once)       : uv run python deployment/seed_firestore.py --project $PROJECT"
echo "  Edit policies / POs         : Firestore console -> policies/ap_rules, policies/fx_rates, purchase_orders/*"
echo "  For future code updates run: bash deployment/deploy.sh"
echo "  Pause/resume intake        : bash deployment/toggle-agentmail-intake.sh"
echo "  Migrating from Gmail?       : bash deployment/cleanup-gmail-infra.sh"
echo "==================================================================="
