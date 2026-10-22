#!/usr/bin/env bash
# Runs at every boot (srt-vast.service): fetch the settings and secrets from SSM
# Parameter Store, pull the code bundle from S3 if it changed since the last
# boot, and bring the production stack up.
set -euo pipefail
source /etc/srt-vast.conf   # REGION, BUCKET, KEY, PARAM_PREFIX (written by cloud-init)

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
docker compose -p srt-vast --env-file "$BASE/.env" \
  -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d "${build[@]}" --remove-orphans
