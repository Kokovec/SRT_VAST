# Deploying to AWS (CloudFront + Cognito, start on sign-in)

One small Graviton EC2 instance runs the whole stack. It is **stopped whenever
nobody is watching** and **started when someone signs in**. Each person has
their own login, and a session lasts **30 minutes**. Everything is in AWS, on an
existing Route 53 domain; only records under the viewer hostname are created.

```
                 ┌────────────── CloudFront (srt.<domain>, ACM certificate) ──────────────┐
browser ───────▶ │ /_auth/*  ─────────────────────────────▶ starter Lambda (sign-in flow) │
                 │ everything else ─▶ origin group:                                        │
                 │     1. the instance  https://origin.srt.<domain>  (Let's Encrypt cert)  │
                 │     2. on 5xx / no answer ─▶ starter Lambda: signed in? start EC2,      │
                 │                                 show "Starting…" page                   │
                 └─────────────────────────────────────────────────────────────────────────┘
                      Cognito hosted login (user pool: only the people you add)
```

| What | How |
|---|---|
| Who gets in | A Cognito user pool where only admins create users (`allowed_emails`). Each person gets an email with a temporary password and chooses their own on first sign-in. |
| Start on sign-in | CloudFront tries the instance first (2 s connect timeout). If it is stopped or still booting, CloudFront fails over to the starter Lambda. The Lambda checks the session cookie (signed-out visitors are sent to sign in first, so only allowed people can wake the instance), starts the instance, and returns a page that reloads until the viewer answers. That takes ≈ 1–2 min, or ≈ 3–5 min on the first start after a code change, because the image rebuilds. |
| Stop on sign-out / idle | `srt-vast-idle.timer` (every 30 s) asks the viewer's `/api/status`; after `idle_minutes` (5) with no page open it powers the OS off, and `instance_initiated_shutdown_behavior = stop` makes that an instance stop. **Log out** ends both the app session and the Cognito login. |
| 30-minute session | The Cognito ID token lives `session_minutes` (30) and is never silently renewed. The viewer verifies it on every request and closes the video and event streams at its expiry; the page then signs the user out, so the next visit asks for the password again. |
| What can reach the instance | Inbound 443 only, and only from CloudFront's origin-facing IP ranges (AWS-managed prefix list). The CloudFront-to-instance hop is HTTPS with a Let's Encrypt certificate for `origin.<hostname>`, obtained and renewed at boot through a Route 53 DNS challenge. Admin shell: SSM Session Manager. |
| Changing IP | The instance gets a new public IP at each start; the boot script updates `origin.<hostname>` (TTL 30 s) before anything else. No Elastic IP to pay for while stopped. |
| Code updates | Terraform zips the repo to a private S3 bucket. At every boot the host re-reads config from SSM Parameter Store and rebuilds only if the bundle changed. So: `terraform apply`, then the next start picks it up. |
| Backstops | The instance also stops if the viewer is not up 15 min after boot (broken stack) or after `max_uptime_hours` (12). |

## Cost

On-demand us-west-2, approximate. Verify with the AWS pricing calculator.

| | Monthly |
|---|---|
| `c7g.large` while running (≈ $0.073/h) | e.g. 40 h ≈ $3 |
| Public IPv4 while running (≈ $0.005/h) | ≈ $0.20 for 40 h |
| 20 GB gp3 root volume (always) | ≈ $1.60 |
| CloudFront (always-free tier: 1 TB out, 10 M requests / month) | $0 at this scale |
| Cognito Lite (free up to 10,000 monthly active users), Lambda, S3 bundle, SSM | ≈ $0 |
| Route 53 records in your existing zone | $0 extra |

Video leaves through CloudFront, so viewing costs nothing until 1 TB/month. Each
viewer pulls the program (up to ≈ 2.6 Mb/s; the test pattern runs nearer 1.1 Mb/s)
plus the incoming preview (≈ 0.24 Mb/s), so the free tier covers roughly 780–1,700
viewer-hours a month. Beyond it, about $0.085/GB (≈ $0.11 per viewer-hour at
the bitrate cap). Data from the instance into CloudFront is free.

## How it is deployed

Two Terraform configurations:

1. **[bootstrap/](bootstrap/)**, applied once with an admin identity, creates:
   - the scoped **`srt-vast-deploy`** role the main config runs as. It can only
     create/modify `srt-vast` resources, change DNS records at or under the
     viewer hostname (never the rest of the zone), and launch small Graviton
     instances in one region;
   - the **`srt-vast-boundary`** permissions boundary, which every role the
     deploy role creates must carry (no privilege escalation);
   - the **Cognito user pool**: Cognito has no tag-based access control, so the
     pool is created here and the deploy role is confined to this one pool's ARN;
   - a small **dedicated VPC** (one public subnet, internet gateway, no NAT
     gateway), so the deploy role needs no network-building rights and the stack
     doesn't depend on a default VPC.
2. **[terraform/](terraform/)**, run as the deploy role, creates everything else.

```bash
# once, as an admin
cd deploy/bootstrap
cat > terraform.tfvars <<EOF
deployer_arns = ["arn:aws:iam::<account>:user/<you>"]
domain        = "example.com"
hostname      = "srt.example.com"
EOF
AWS_PROFILE=<admin profile> terraform init && AWS_PROFILE=<admin profile> terraform apply

# the stack, as the deploy role (profile with role_arn = the deploy role)
cd ../terraform
cp terraform.tfvars.example terraform.tfvars   # emails + the bootstrap outputs
AWS_PROFILE=srt-vast terraform init && AWS_PROFILE=srt-vast terraform apply
```

No Terraform installed? Run it from Docker, e.g.
`docker run --rm -it --dns 1.1.1.1 --dns 8.8.8.8 -v "$PWD/../..:/repo" -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=srt-vast -w /repo/deploy/terraform hashicorp/terraform:1.9.8 apply`.
The `--dns` flags work around WSL's DNS forwarder, which intermittently drops lookups from containers.

The first boot installs Docker, builds the image and renders the ad creatives
(≈ 5–10 min), then the instance stops itself after the idle period. Open the
`url` output, sign in, and the instance starts.

- **Add or remove people:** edit `allowed_emails`, `terraform apply`. New people
  get an invitation email (Cognito's built-in sender: ≈ 50 emails/day). A removed
  person's current session still runs to its 30-minute end.
- **Ship code changes:** `terraform apply` uploads a new bundle (the plan should
  show only `aws_s3_object.bundle`). The next start rebuilds, adding a few minutes.
  To apply to a running instance now, restart the stack over SSM (this interrupts
  viewers while the image rebuilds):
  `aws ssm send-command --document-name AWS-RunShellScript --instance-ids <id> --parameters 'commands=["systemctl restart srt-vast"]'`.
- **Logs:** on the host `journalctl -u srt-vast -u srt-vast-idle` and
  `sudo docker compose -p srt-vast logs -f stitcher`; the starter Lambda logs to
  CloudWatch `/aws/lambda/srt-vast-starter`.
- **Stitcher health:** the stitcher logs `input stalled` / `slow processing`
  whenever its input pauses or one chunk takes over 300 ms.
- **Tear down:** `terraform destroy` in `terraform/`, then in `bootstrap/` (the
  user pool has deletion protection on; turn it off first).

## Troubleshooting

- **The invitation's password doesn't work.** Gmail's "Code requested" card
  can pick up the wrong characters. Use the password from the email body.
  An admin can also set a fresh temporary one, which the user must replace
  on first sign-in:
  `aws cognito-idp admin-set-user-password --user-pool-id <pool> --username <email> --password <temp> --no-permanent`.
  To send a new invitation instead: `aws cognito-idp admin-create-user --user-pool-id <pool> --username <email> --message-action RESEND`.
- **"Forgot password" never sends an email.** Cognito sends reset codes only
  to users who have completed their first sign-in (status `FORCE_CHANGE_PASSWORD`
  doesn't qualify), and it says nothing, to avoid revealing which users exist.
  Use one of the options above.
- **CloudFront `InvalidViewerCertificate` with a valid, issued certificate.**
  Attaching an ACM certificate makes a KMS call on the caller's behalf in
  us-east-1. The deploy role's region pin exempts `kms:*` for that reason.
- **Terraform wants to replace a stopped instance.** A stopped instance has no
  public IP, so `associate_public_ip_address` reads `false`. The config takes
  the public IP from the subnet and ignores that attribute.

## Security notes

- Terraform state holds the Cognito app client secret. Keep it local or in an
  encrypted, access-controlled backend, never in git (`.gitignore` covers it).
- The viewer and the starter Lambda accept only RS256 ID tokens from this user
  pool for this app client, checked against the pool's published keys.
- The starter Lambda is a function URL with IAM auth; only this CloudFront
  distribution may invoke it (origin access control + resource policy). Its role
  can describe instances and start this one.
- The host role can read its own bundle object and parameters, and change only
  its `origin.` A record and the Let's Encrypt `_acme-challenge` TXT record.
- The raw SRT outputs are not exposed.
