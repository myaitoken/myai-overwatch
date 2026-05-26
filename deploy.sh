#!/usr/bin/env bash
# deploy.sh — Ship myai-overwatch to the AWS bridge
# Usage: ./deploy.sh
# Mirrors the pattern used by other Infinihash services.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICE="myai-overwatch"
S3_BUCKET="infinihash-backups-47250d97"
S3_KEY="deploy/${SERVICE}.tar.gz"
INSTANCE_ID="i-0ee15650c3fa2eec7"   # AWS bridge
CONTAINER="${SERVICE}"               # Incus container name on the bridge
INSTALL_DIR="/opt/${SERVICE}"

GATEWAY_URL="${GATEWAY_URL:-https://gateway.infinihash.com}"
GATEWAY_TOKEN="${GATEWAY_TOKEN:-$(cat "${REPO_ROOT}/../infra-gateway/bearer-token.txt" 2>/dev/null || echo "")}"

echo "=== Packing ${SERVICE} ==="
cd "${REPO_ROOT}"
COPYFILE_DISABLE=1 tar \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='venv' \
  -czf "/tmp/${SERVICE}.tar.gz" .

echo "=== Uploading to s3://${S3_BUCKET}/${S3_KEY} ==="
aws s3 cp "/tmp/${SERVICE}.tar.gz" "s3://${S3_BUCKET}/${S3_KEY}" \
    --region us-east-2

echo "=== Installing on bridge via SSM+Incus ==="
SSM_CMD=$(cat <<EOF
set -euo pipefail

# ── Create container if it doesn't exist ──────────────────────────────────
if ! incus info ${CONTAINER} &>/dev/null; then
  echo "Creating container ${CONTAINER}..."
  incus launch images:debian/12 ${CONTAINER}
  sleep 5
fi

# ── Extract fresh code ─────────────────────────────────────────────────────
aws s3 cp s3://${S3_BUCKET}/${S3_KEY} /tmp/${SERVICE}.tar.gz --region us-east-2
incus file push /tmp/${SERVICE}.tar.gz ${CONTAINER}/tmp/${SERVICE}.tar.gz

incus exec ${CONTAINER} -- bash -c "
  set -euo pipefail
  mkdir -p ${INSTALL_DIR}

  # Preserve .env if it exists
  [ -f ${INSTALL_DIR}/.env ] && cp ${INSTALL_DIR}/.env /tmp/.env.bak

  # Extract
  tar -xzf /tmp/${SERVICE}.tar.gz -C ${INSTALL_DIR}

  # Restore .env
  [ -f /tmp/.env.bak ] && cp /tmp/.env.bak ${INSTALL_DIR}/.env

  # Python venv + deps
  if [ ! -d ${INSTALL_DIR}/venv ]; then
    python3 -m venv ${INSTALL_DIR}/venv
  fi
  ${INSTALL_DIR}/venv/bin/pip install -q -r ${INSTALL_DIR}/requirements.txt

  # State dir
  mkdir -p /var/lib/${SERVICE}

  # System user
  id overwatch &>/dev/null || useradd -r -s /bin/false overwatch
  chown -R overwatch:overwatch /var/lib/${SERVICE}

  # Systemd
  cp ${INSTALL_DIR}/overwatch.service /etc/systemd/system/${SERVICE}.service
  systemctl daemon-reload
  systemctl enable ${SERVICE}
  systemctl restart ${SERVICE}
  sleep 2
  systemctl is-active ${SERVICE} && echo 'overwatch is UP' || (journalctl -u ${SERVICE} -n 20; exit 1)
"
EOF
)

aws ssm send-command \
    --region us-east-2 \
    --instance-ids "${INSTANCE_ID}" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"${SSM_CMD//\"/\\\"}\"]" \
    --comment "Deploy ${SERVICE}" \
    --output text \
    --query 'Command.CommandId'

echo ""
echo "=== Registering with infra-gateway ==="
echo "(If registry.py already has '${SERVICE}' entry, this is a no-op)"

echo ""
echo "✅ Deploy triggered. Check status:"
echo "   curl -s https://gateway.infinihash.com/logs/${SERVICE} -H 'Authorization: Bearer \$GATEWAY_TOKEN'"
