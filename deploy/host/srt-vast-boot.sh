#!/usr/bin/env bash
# Runs at every boot (srt-vast.service):
#   1. point origin.<hostname> (the CloudFront origin) at this boot's public IP;
#   2. get/renew the origin's TLS certificate (Let's Encrypt, Route 53 DNS challenge);
#   3. fetch settings from SSM Parameter Store and the code bundle from S3
#      (only when it changed);
#   4. bring the production stack up.
set -euo pipefail
source /etc/srt-vast.conf   # REGION BUCKET KEY PARAM_PREFIX ZONE_ID ORIGIN_HOSTNAME (from cloud-init)

imds() {
  local token
  token=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
  curl -fsS -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1"
}

ip=$(imds public-ipv4)
echo "origin $ORIGIN_HOSTNAME -> $ip"
aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch "$(printf \
  '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"%s","Type":"A","TTL":30,"ResourceRecords":[{"Value":"%s"}]}}]}' \
  "$ORIGIN_HOSTNAME" "$ip")" > /dev/null

certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
  --dns-route53 --keep-until-expiring -d "$ORIGIN_HOSTNAME"

BASE=/opt/srt-vast
APP=$BASE/app
mkdir -p "$BASE"

# Settings and secrets -> .env (read by docker compose for variable substitution).
aws ssm get-parameters-by-path --region "$REGION" --path "$PARAM_PREFIX" --with-decryption \
    --query 'Parameters[].[Name,Value]' --output text \
  | while IFS=$'\t' read -r name value; do printf '%s=%s\n' "${name##*/}" "$value"; done > "$BASE/.env.new"
chmod 600 "$BASE/.env.new"
mv "$BASE/.env.new" "$BASE/.env"

# Code bundle: only download and rebuild when Terraform uploaded a new one.
build=()
etag=$(aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$KEY" --query ETag --output text)
if [[ "$etag" != "$(cat "$BASE/bundle.etag" 2>/dev/null || true)" || ! -d "$APP" ]]; then
  echo "new code bundle $etag"
  aws s3 cp --region "$REGION" --only-show-errors "s3://$BUCKET/$KEY" "$BASE/bundle.zip"
  rm -rf "$APP.new" && mkdir -p "$APP.new"
  unzip -q "$BASE/bundle.zip" -d "$APP.new"
  rm -rf "$APP" && mv "$APP.new" "$APP"
  echo "$etag" > "$BASE/bundle.etag"
  build=(--build)
fi

cd "$APP"
compose=(docker compose -p srt-vast --env-file "$BASE/.env" -f docker-compose.yml -f deploy/docker-compose.prod.yml)
"${compose[@]}" up -d "${build[@]}" --remove-orphans
# Docker starts the containers at boot before this script renews the certificate;
# restart the viewer so it serves the current one.
"${compose[@]}" restart viewer
