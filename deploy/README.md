# Deploying to AWS behind Cloudflare Access

One small Graviton EC2 instance runs the whole stack. It is **stopped whenever
nobody is watching** and **started when someone signs in**. Access is per-person
(email allow-list) with a **30-minute session**.

```
browser ──▶ Cloudflare Access ──▶ Worker "waker" ──▶ Tunnel ──▶ EC2 (no inbound ports)
            email code login      origin down?        cloudflared ──▶ viewer :8090
            30-min session          └─▶ EC2 StartInstances, "Starting…" page
```

| What | How |
|---|---|
| Who gets in | Cloudflare Access app on the hostname, policy = `allowed_emails`. Login with a one-time code sent by email. |
| Start on sign-in | Every request passes through the Worker (after Access). If the tunnel has no connector (instance stopped or booting) it calls `ec2:StartInstances` with an IAM user that may only start **this** instance, and shows a page that reloads until the viewer is up (≈1–2 min cold, longer on the very first boot while the image builds). |
| Stop on sign-out / idle | `srt-vast-idle.timer` (every 30 s) asks the viewer's `/api/status`; after `idle_minutes` (5) with no page open it powers the OS off, and `instance_initiated_shutdown_behavior = stop` turns that into an instance stop. The page's **Log out** button ends the Access session, so its streams close and the idle clock starts. |
| 30-minute session | Access `session_duration = 30m`. The viewer verifies the Access JWT on every request and closes the video and event streams at the token's expiry; the page then returns to sign-in. Streams that were already open do not outlive the session. |
| Backstops | The instance also stops if the viewer is not up 15 min after boot (broken stack) or after `max_uptime_hours` (12). |
| No open ports | Security group has no inbound rules. The viewer listens on the host's loopback only (for the watchdog); everything else stays on the Docker network. Admin shell: SSM Session Manager. |
| Code updates | Terraform zips the repo to a private S3 bucket. At every boot the host re-reads config/secrets from SSM Parameter Store, and rebuilds only if the bundle changed. So: `terraform apply`, then the next start picks it up. |

## Cost

On-demand us-east-1, approximate. Verify with the AWS pricing calculator.

| | Monthly |
|---|---|
| `c7g.large` while running (≈ $0.073/h) | ≈ $0.07 × hours used, e.g. 40 h ≈ $3 |
| Public IPv4 while running (≈ $0.005/h) | ≈ $0.20 for 40 h |
| 20 GB gp3 root volume (always) | ≈ $1.60 |
| S3 bundle, SSM parameters | ≈ $0 |
| Cloudflare Tunnel, Access (≤ 50 users), Worker (≤ 100k req/day) | $0 |
| Data out: ≈ 1.2 GB per viewer-hour, first 100 GB/month free | $0.09/GB beyond that |

## Prerequisites

- An AWS account and credentials for Terraform (`aws configure` / env vars).
- A Cloudflare account with a domain (zone) on it, and a Zero Trust team
  (free plan; the team domain looks like `myteam.cloudflareaccess.com`).
- A Cloudflare API token (`CLOUDFLARE_API_TOKEN`) with:
  Account › Cloudflare Tunnel: Edit, Account › Access: Apps and Policies: Edit,
  Account › Access: Organizations, Identity Providers, and Groups: Edit,
  Account › Workers Scripts: Edit, Zone › Workers Routes: Edit, Zone › DNS: Edit.
- Terraform ≥ 1.6, or Docker: `docker run --rm -it -v "$PWD:/repo" -w /repo/deploy/terraform -e CLOUDFLARE_API_TOKEN -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY hashicorp/terraform:1.9.8 <command>`.

## Deploy

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in account/zone IDs, hostname, team domain, emails
terraform init
terraform apply
```

The first boot installs Docker, builds the image and renders the ad creatives
(≈ 5–10 min); the instance then stops itself after the idle period. Open the
`url` output, sign in with an allowed email, and the instance starts.

- **Add or remove people:** edit `allowed_emails`, `terraform apply`. Removal is
  effective at the person's next request; to cut a live session immediately,
  revoke it in Zero Trust › Access › Applications.
- **Ship code changes:** `terraform apply` (uploads a new bundle); the next start
  rebuilds. To apply right away: `aws ssm start-session --target <instance>` then
  `sudo systemctl restart srt-vast`.
- **Logs on the host:** `journalctl -u srt-vast -u srt-vast-idle`,
  `sudo docker compose -p srt-vast logs -f stitcher`.
- **Tear down:** `terraform destroy`.

## Security notes

- Terraform state contains the tunnel token and the Worker's AWS key. Keep it
  local or in an encrypted, access-controlled backend (S3 + DynamoDB lock, or
  Terraform Cloud), never in git (`.gitignore` covers the local files).
- The Worker's IAM user can describe instances (EC2 has no resource scoping for
  that) and start this one instance; it cannot stop, modify or create anything.
- The instance role can read only its own bundle object and parameter path.
- The viewer trusts nothing but a valid Access JWT for this application's AUD,
  checked against the team's published signing keys, so a request that somehow
  bypassed Cloudflare would still be refused.
- The raw SRT outputs are not exposed. If outside players need SRT, publish it
  separately with an SRT passphrase plus a security-group IP allow-list
  (SRT has no per-user credentials).
