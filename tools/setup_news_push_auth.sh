#!/usr/bin/env bash
#
# One-time setup so news-push.yml can send FCM messages from GitHub Actions.
#
# WHY THIS EXISTS
# ---------------
# news-push.yml authenticates with Workload Identity Federation and needs two
# repository secrets that do not exist yet:
#
#     GCP_WORKLOAD_IDENTITY_PROVIDER
#     GCP_SERVICE_ACCOUNT
#
# Without them the auth step fails with "must specify exactly one of
# workload_identity_provider or credentials_json", the job goes red, and no
# notification is sent — which is exactly what happened on the 17-Aug-2026
# publish of news 4.0.0. The data reached users; the alert did not.
#
# NEITHER SECRET IS A CREDENTIAL. One is a resource path, the other an email
# address. That is the entire point of WIF: GitHub proves its identity with a
# short-lived OIDC token and no long-lived key is ever stored in the repo.
# If you find yourself pasting a service-account JSON key somewhere, stop —
# that is the design this deliberately avoids.
#
# BEFORE RUNNING
# --------------
#   gcloud auth login
#   gcloud config set project kredme-c6206
#
# You need Owner, or all of: Service Account Admin, Workload Identity Pool
# Admin, Role Admin, and Service Usage Admin on kredme-c6206.
#
# Safe to re-run: every step tolerates the resource already existing.

set -euo pipefail

PROJECT_ID="kredme-c6206"
PROJECT_NUMBER="136378395162"     # from android/app/google-services.json
REPO="kredme-data/kredme-data"
POOL="github"
PROVIDER="kredme-data"
SA_NAME="news-push"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
ROLE_ID="fcmSendOnly"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

say "Checking you are authenticated"
if ! gcloud auth list --format='value(account)' 2>/dev/null | grep -q .; then
  echo "No gcloud credentials. Run:  gcloud auth login" >&2
  exit 2
fi
echo "authenticated as: $(gcloud auth list --filter=status:ACTIVE --format='value(account)')"

say "Enabling the APIs WIF and FCM need"
# iamcredentials + sts are what the OIDC exchange itself runs on; without them
# the token swap fails with a confusing 403 rather than a missing-API error.
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  fcm.googleapis.com \
  --project="${PROJECT_ID}"

say "Creating the service account (skipped if present)"
gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="KredMe news push (GitHub Actions)" \
  --description="Sends the one FCM topic message published by news-push.yml. No key; reached only via Workload Identity Federation from ${REPO}."

say "Creating a send-only custom role (skipped if present)"
# cloudmessaging.messages.create is the single permission FCM's
# v1/projects/*/messages:send requires. The predefined alternative,
# roles/firebasemessaging.admin, also grants read and delete on messaging
# resources that this workflow has no business touching.
gcloud iam roles describe "${ROLE_ID}" --project="${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam roles create "${ROLE_ID}" \
  --project="${PROJECT_ID}" \
  --title="FCM send only" \
  --description="Send an FCM message. Nothing else." \
  --permissions="cloudmessaging.messages.create" \
  --stage=GA

say "Granting that role to the service account"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="projects/${PROJECT_ID}/roles/${ROLE_ID}" \
  --condition=None \
  --quiet >/dev/null

say "Creating the Workload Identity Pool (skipped if present)"
gcloud iam workload-identity-pools describe "${POOL}" \
  --project="${PROJECT_ID}" --location=global >/dev/null 2>&1 || \
gcloud iam workload-identity-pools create "${POOL}" \
  --project="${PROJECT_ID}" --location=global \
  --display-name="GitHub Actions"

say "Creating the OIDC provider (skipped if present)"
# ⚠️ THE ATTRIBUTE CONDITION IS LOAD-BEARING SECURITY, NOT A NICETY.
# Without it, ANY GitHub repository on earth can mint a token this pool
# accepts and impersonate the service account. The condition pins the
# exchange to this one repo. Google now refuses to create a provider that
# maps attribute.repository without one — that refusal is protecting you.
#
# Not pinned to refs/heads/main on purpose: news-push.yml also offers a
# workflow_dispatch dry run, which a person may reasonably trigger from a
# branch. The send itself is gated in the workflow, not here.
gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
  --project="${PROJECT_ID}" --location=global \
  --workload-identity-pool="${POOL}" >/dev/null 2>&1 || \
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
  --project="${PROJECT_ID}" --location=global \
  --workload-identity-pool="${POOL}" \
  --display-name="${REPO}" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository == '${REPO}'"

say "Letting ONLY ${REPO} impersonate the service account"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" \
  --quiet >/dev/null

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

say "Setting the two GitHub repository secrets"
if command -v gh >/dev/null 2>&1; then
  gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "${REPO}" --body "${WIF_PROVIDER}"
  gh secret set GCP_SERVICE_ACCOUNT          --repo "${REPO}" --body "${SA_EMAIL}"
  echo "both secrets set on ${REPO}"
else
  echo "gh not found — set these two by hand at"
  echo "  https://github.com/${REPO}/settings/secrets/actions"
  echo "    GCP_WORKLOAD_IDENTITY_PROVIDER = ${WIF_PROVIDER}"
  echo "    GCP_SERVICE_ACCOUNT            = ${SA_EMAIL}"
fi

cat <<EOF

Done.

  provider  ${WIF_PROVIDER}
  account   ${SA_EMAIL}

Verify WITHOUT notifying anyone — dry_run defaults to true, which exercises
auth and the message build but sends nothing:

  gh workflow run news-push.yml --repo ${REPO} -f dry_run=true
  gh run watch \$(gh run list --workflow=news-push.yml --repo ${REPO} --limit 1 --json databaseId --jq '.[0].databaseId') --repo ${REPO}

A green dry run means auth works. It does NOT mean anyone will receive a
notification: FCM returns 200 for a topic with zero subscribers, and
subscription to 'news_prod' only begins on the first launch of an app build
containing FcmService._subscribeToNewsTopic. Measure reach in Firebase, never
from this workflow's exit code.
EOF
