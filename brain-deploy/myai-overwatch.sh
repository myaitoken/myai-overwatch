#!/usr/bin/env bash
# /opt/deploy/myai-overwatch.sh — runs ON Brain (CT103)
# Called by infra-gateway when POST /deploy/myai-overwatch is received.
set -euo pipefail

SERVICE="myai-overwatch"
REPO="https://github.com/myaitoken/${SERVICE}.git"
WORK_DIR="/tmp/deploy-${SERVICE}-$$"
S3_BUCKET="infinihash-backups-47250d97"
S3_KEY="deploy/${SERVICE}.tar.gz"
INSTANCE_ID="i-0ee15650c3fa2eec7"
CONTAINER="${SERVICE}"
INSTALL_DIR="/opt/${SERVICE}"
PROFILE="${AWS_PROFILE:-infinihash}"

echo "[overwatch-deploy] Cloning ${REPO}"
git clone --depth=1 "${REPO}" "${WORK_DIR}"

echo "[overwatch-deploy] Packing"
COPYFILE_DISABLE=1 tar \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='venv' --exclude='brain-deploy' \
  -czf "/tmp/${SERVICE}.tar.gz" -C "${WORK_DIR}" .

echo "[overwatch-deploy] Uploading to S3"
aws s3 cp "/tmp/${SERVICE}.tar.gz" "s3://${S3_BUCKET}/${S3_KEY}" --region us-east-2 --profile "${PROFILE}"

# Heredoc quoting: inner single-quotes are fine; outer bash will expand $VARS
REMOTE=$(cat <<REMOTE
#!/bin/bash
set -euo pipefail

# Create container if missing
if ! incus info ${CONTAINER} &>/dev/null; then
  echo "Creating ${CONTAINER}..."
  incus launch images:debian/12 ${CONTAINER}
  sleep 8
  incus exec ${CONTAINER} -- apt-get update -qq
  incus exec ${CONTAINER} -- apt-get install -y -qq python3 python3-pip python3-venv awscli
fi

# Pull tarball
aws s3 cp s3://${S3_BUCKET}/${S3_KEY} /tmp/${SERVICE}.tar.gz --region us-east-2
incus file push /tmp/${SERVICE}.tar.gz ${CONTAINER}/tmp/${SERVICE}.tar.gz

incus exec ${CONTAINER} -- bash -c '
  set -euo pipefail
  INSTALL_DIR="${INSTALL_DIR}"

  mkdir -p "\$INSTALL_DIR"
  [ -f "\$INSTALL_DIR/.env" ] && cp "\$INSTALL_DIR/.env" /tmp/.env.bak

  tar -xzf /tmp/${SERVICE}.tar.gz -C "\$INSTALL_DIR"

  [ -f /tmp/.env.bak ] && cp /tmp/.env.bak "\$INSTALL_DIR/.env"

  [ -d "\$INSTALL_DIR/venv" ] || python3 -m venv "\$INSTALL_DIR/venv"
  "\$INSTALL_DIR/venv/bin/pip" install -q -r "\$INSTALL_DIR/requirements.txt"

  mkdir -p /var/lib/${SERVICE}
  id overwatch &>/dev/null || useradd -r -s /bin/false overwatch
  chown -R overwatch:overwatch /var/lib/${SERVICE}

  cp "\$INSTALL_DIR/overwatch.service" /etc/systemd/system/${SERVICE}.service
  systemctl daemon-reload
  systemctl enable ${SERVICE}
  systemctl restart ${SERVICE}
  sleep 3
  systemctl is-active ${SERVICE} && echo "[overwatch-deploy] UP ✓" || {
    journalctl -u ${SERVICE} -n 30
    exit 1
  }
'
REMOTE
)

echo "[overwatch-deploy] Deploying via SSM"
aws ssm send-command \
    --region us-east-2 \
    --profile "${PROFILE}" \
    --instance-ids "${INSTANCE_ID}" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"$(echo "$REMOTE" | sed 's/"/\\"/g')\"]" \
    --comment "Deploy ${SERVICE}" \
    --output text \
    --query 'Command.CommandId'

rm -rf "${WORK_DIR}" "/tmp/${SERVICE}.tar.gz"
echo "[overwatch-deploy] Done"
