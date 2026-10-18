#!/usr/bin/env bash
# Pause/resume live AgentMail-triggered invoice intake without redeploying code.
#
# Flips AGENTMAIL_INTAKE_ENABLED on the Cloud Run service (which rolls a new
# revision). While paused the WebSocket listener is not started; the UI, A2A
# endpoint and any run already in progress keep working.
#
# Unlike the old Pub/Sub setup there is no queue holding mail while paused. On
# resume the listener only catches up on mail newer than AGENTMAIL_CATCHUP_MINUTES
# (default 30). To pick up a longer backlog, pass a window on resume:
#   bash deployment/toggle-agentmail-intake.sh resume 240
#
# Usage: GCP_PROJECT_ID=your-project \
#          bash deployment/toggle-agentmail-intake.sh pause|resume [catchup_minutes]|status

set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your GCP project id}"
REGION="us-west1"
SERVICE="invio-invoice-processing-agent-demo"

usage() {
  echo "Usage: bash deployment/toggle-agentmail-intake.sh pause|resume [catchup_minutes]|status" >&2
  exit 1
}

[[ $# -ge 1 && $# -le 2 ]] || usage

case "$1" in
  pause)
    gcloud run services update "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --update-env-vars="AGENTMAIL_INTAKE_ENABLED=false"
    echo "Paused: the AgentMail listener is off. Mail received now is only picked up on resume if it falls inside the catch-up window."
    ;;
  resume)
    ENV_VARS="AGENTMAIL_INTAKE_ENABLED=true"
    if [[ $# -eq 2 ]]; then
      [[ "$2" =~ ^[0-9]+$ ]] || usage
      ENV_VARS="${ENV_VARS},AGENTMAIL_CATCHUP_MINUTES=$2"
    fi
    gcloud run services update "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --update-env-vars="$ENV_VARS"
    echo "Resumed: the AgentMail listener will connect and catch up on unhandled mail."
    ;;
  status)
    gcloud run services describe "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --format="value(spec.template.spec.containers[0].env)" \
      | tr ';' '\n' | grep -E "AGENTMAIL_(INTAKE_ENABLED|CATCHUP_MINUTES|INBOX_ID)" || true
    URL=$(gcloud run services describe "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --format="value(status.url)")
    echo "Live listener state: curl ${URL}/intake/status"
    ;;
  *)
    usage
    ;;
esac
