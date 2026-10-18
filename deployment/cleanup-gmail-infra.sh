#!/usr/bin/env bash
# Remove the retired Gmail intake infrastructure from a project that previously
# ran the Gmail + Pub/Sub version of this service.
#
# Run this only AFTER the AgentMail revision is deployed and
# <service-url>/intake/status reports connected=true. It is destructive and is
# NOT part of deploy-infra.sh; it does nothing unless CONFIRM=yes is set.
#
# Removes: Pub/Sub push subscription + topic, Cloud Scheduler watch-renew job,
# the invoker service account, the Gmail OAuth secret, and the stale Gmail env
# vars on the Cloud Run service. The Gmail API watch needs no teardown: it stops
# publishing once the topic is gone and expires on its own within 7 days.
#
# Usage: GCP_PROJECT_ID=your-project CONFIRM=yes bash deployment/cleanup-gmail-infra.sh

set -uo pipefail

PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your GCP project id}"
REGION="us-west1"
SERVICE="invio-invoice-processing-agent-demo"
TOPIC="gmail-invoices"
SUB="gmail-invoices-push"
SCHEDULER_JOB="gmail-watch-renew"
SA_INVOKER="ipa-pubsub-invoker"
SECRET_NAME="gmail-oauth-token"

echo "This will delete from project '$PROJECT':"
echo "  - Pub/Sub subscription $SUB and topic $TOPIC (undelivered messages are lost)"
echo "  - Cloud Scheduler job $SCHEDULER_JOB"
echo "  - Service account $SA_INVOKER"
echo "  - Secret $SECRET_NAME (the Gmail OAuth refresh token — all versions)"
echo "  - Gmail env vars on Cloud Run service $SERVICE"

if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo ""
  echo "Dry run only. Re-run with CONFIRM=yes to proceed."
  exit 0
fi

gcloud pubsub subscriptions delete "$SUB" --project="$PROJECT" --quiet || echo "  (subscription not found)"
gcloud pubsub topics delete "$TOPIC" --project="$PROJECT" --quiet || echo "  (topic not found)"
gcloud scheduler jobs delete "$SCHEDULER_JOB" --location="$REGION" --project="$PROJECT" --quiet || echo "  (scheduler job not found)"
gcloud iam service-accounts delete "${SA_INVOKER}@${PROJECT}.iam.gserviceaccount.com" --project="$PROJECT" --quiet || echo "  (invoker SA not found)"
gcloud secrets delete "$SECRET_NAME" --project="$PROJECT" --quiet || echo "  (secret not found)"
gcloud run services update "$SERVICE" \
  --region="$REGION" --project="$PROJECT" \
  --remove-env-vars="GMAIL_OAUTH_SECRET,GMAIL_WATCH_TOPIC,GMAIL_ACCOUNT,PUBSUB_PUSH_AUDIENCE,EXPECTED_INVOKER_SA,PUBSUB_SKIP_AUTH" || echo "  (could not update env vars)"

echo "Gmail infrastructure removed."
