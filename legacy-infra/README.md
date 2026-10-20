# The legacy estate: Inventory & Orders on one EC2 instance

This is the "before" picture of the workshop. **Legacy Inventory & Orders** is a Flask monolith written in 2017
and "temporarily" deployed by hand on a single EC2 instance. It has a JSON API that the warehouse scanners use,
a small HTML dashboard, a SQLite database on the root volume, and a nightly cron job that writes a CSV report to local disk.

The Terraform in `terraform/` rebuilds that server exactly as it was set up, smells included, so that the
agents have a real estate to discover, assess, modernize and right-size.

```
             Internet ── HTTP :80 (var.allowed_cidr, default 0.0.0.0/0)
                            │
┌─ us-west-2 · default VPC · one public subnet (single AZ) ─────────────────┐
│                           ▼                                               │
│  ┌─ EC2 legacy-inventory · t3.large · Amazon Linux 2023 · gp2 30 GiB ──┐  │
│  │  nginx :80 ──► Flask dev server :5000 (python3.9, systemd, root)    │  │
│  │                  │                 │                    │           │  │
│  │                  ▼                 ▼                    ▼           │  │
│  │      data/inventory.db     reports/*.csv     /var/log/.../app.log   │  │
│  │      (SQLite)                      ▲                                │  │
│  │  cron 02:00 ── curl /reports/daily ┘                                │  │
│  │  SSM agent (shell access) · CloudWatch agent (memory, disk)         │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
│  IAM role: AmazonSSMManagedInstanceCore + CloudWatchAgentServerPolicy     │
└───────────────────────────────────────────────────────────────────────────┘
  No load balancer · no Auto Scaling group · no SSH key · local Terraform state
```

## What gets created

| Resource | Notes |
|---|---|
| `aws_instance.legacy` | t3.large, latest AL2023 AMI (SSM public parameter), gp2 30 GiB, IMDSv2, public IP, tags `Name`/`app`=`legacy-inventory`, `project` |
| `aws_iam_role` + `aws_iam_instance_profile` | `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy` |
| `aws_security_group` + 2 rules | port 80 in from `var.allowed_cidr`, all egress, **no port 22** |

That's 8 resources and nothing else: no S3 bucket and no key pair. State is a local file (`terraform/terraform.tfstate`).

**How the app gets onto the instance.** Terraform embeds every file in `app/` into the user_data script, each one
gzip+base64 encoded (`base64gzip()` in `main.tf`), and the bash bootstrap (`user_data.sh.tftpl`) writes them back out.
That beats S3 for a live demo: there are fewer moving parts (no bucket, no IAM grant for it, nothing left over on destroy), and the only things fetched
at boot are the dnf and PyPI packages. The encoding is byte-stable, so plans don't drift between checkouts,
and `user_data_replace_on_change` rebuilds the server whenever the app changes. The rendered script is about 9 KB.
A precondition stops the plan if it ever gets close to the 16 KB EC2 limit.

**The bootstrap** (logs to `/var/log/legacy-bootstrap.log`, safe to re-run) does the following: it installs nginx, cronie and python3-pip; writes the app to
`/opt/legacy-inventory/app`; runs `pip3 install` into the system Python (as root, the 2017 way); installs the systemd unit,
nginx site and cron file from `app/ops/`; removes the stock nginx server block; and starts everything. As a best-effort extra, it also sets up the CloudWatch agent
for `mem_used_percent` and disk `used_percent` (namespace `CWAgent`). If that step fails, it only logs a warning.

## Deploy, use, destroy

```bash
export AWS_PROFILE=personal-dev AWS_REGION=us-west-2

scripts/legacy-up.sh                    # init + plan, asks for confirmation, applies, waits until the app answers (~3 min)
ALLOWED_CIDR="$(curl -s https://checkip.amazonaws.com)/32" scripts/legacy-up.sh   # lock port 80 to your IP
scripts/legacy-load.sh                  # 5 minutes of mixed GET/POST traffic (args: [URL] [MINUTES], env WORKERS=4)
scripts/legacy-down.sh                  # terraform destroy (asks you to type "yes")
```

Useful outputs (`terraform -chdir=legacy-infra/terraform output`): `app_url`, `instance_id`, `instance_type`,
`ssm_session_command`, `smoke_test`. To get a shell, run `aws ssm start-session --target <instance_id>` (needs the
Session Manager plugin), then `sudo tail -f /var/log/legacy-bootstrap.log`, `systemctl status legacy-inventory`,
or `ls /opt/legacy-inventory/reports`.

You can also run Terraform directly: `cd terraform && terraform init && terraform apply`. To change variables, copy `terraform.tfvars.example` to
`terraform.tfvars`, which git ignores.

**Test the bootstrap without AWS.** `terraform/tests/bootstrap_in_docker.sh` renders the real user_data with Terraform,
runs it twice in `amazonlinux:2023` (no systemd there, so the script starts the processes directly), then checks `/`,
`/api/items`, `POST /api/orders` (201, then 409 on oversell), the report, the cron file and the logs through nginx. It takes about 1 minute.

## Cost

Prices from the AWS Pricing API, us-west-2, on-demand, October 2026.

| Item | USD/day | USD/month |
|---|---:|---:|
| EC2 t3.large ($0.0832/h) | 2.00 | 60.74 |
| EBS gp2 30 GiB ($0.10/GB-month) | 0.10 | 3.00 |
| Public IPv4 address ($0.005/h) | 0.12 | 3.65 |
| CloudWatch agent, 2 custom metrics ($0.30/metric-month) | 0.02 | 0.60 |
| **Total** | **~2.24** | **~68** |

Excluded: data transfer (negligible), and t3 "unlimited" surplus credits (only charged if CPU stays above the 30% baseline,
which this app never does). **Destroy it after the session.**

## Deliberate legacy smells

These are on purpose; the agents are supposed to find them. Paths are relative to `legacy-infra/`.

| # | Smell | Where | Why it hurts | Requirement |
|---|---|---|---|---|
| 1 | Hard-coded secrets | `app/app.py:16-17` (`SECRET_KEY`, `ADMIN_TOKEN = "admin123"`) | Credentials sit in source control, and in the EC2 user data too | SEC1 |
| 2 | SQL injection in search | `app/app.py:77` (string concatenation into `LIKE`) | `?search=x' UNION SELECT 1, name, sql, 0, 0 FROM sqlite_master --` dumps the schema | SEC5 |
| 3 | Flask dev server running as root | `app/app.py:149` (`app.run`), `app/ops/legacy-inventory.service:6,8` (`User=root`) | Werkzeug isn't a production server; a compromise means root on the box | SEC4 |
| 4 | SQLite on local disk | `app/app.py:13,29` | Single writer, no backups, and the data dies with the instance or volume | TC3, NFR4 |
| 5 | Reports written to local disk | `app/app.py:14,128-138` | CSVs pile up on the root volume ("the disk fills up") | FR3 |
| 6 | File logging | `app/app.py:15,23` | No rotation, no central logs, and every request line lands in `app.log` | NFR5 |
| 7 | No health check | `app/app.py:141-143` (`/status` exists but nothing probes it), `terraform/main.tf:122` (no LB/ASG), `app/ops/legacy-inventory.service:9` | systemd restarts on a crash only; nobody notices a hung app | NFR6 |
| 8 | Oversized instance | `terraform/variables.tf:16` (`t3.large`) | 2 vCPU / 8 GiB for an app at a few percent CPU | G3 |
| 9 | gp2 volume | `terraform/variables.tf:22` | gp3 is about 20% cheaper and has a 3000 IOPS baseline | G3 |
| 10 | Single AZ, single instance | `terraform/main.tf:122-125` (one instance, `sort(...)[0]` = one subnet) | Losing the AZ or the instance means an outage, with no redundancy at all | G2, NFR1 |
| 11 | Manual cron | `app/ops/legacy-inventory.cron:2` | Tied to one box, with no retries, no alerting, and output that goes to a log file | FR3 |

More findings for keen eyes:

| # | Smell | Where | Why it hurts |
|---|---|---|---|
| 12 | Check-then-act stock update | `app/app.py:116-119` | `SELECT` then `UPDATE` in separate statements, so concurrent orders can oversell (FR4) |
| 13 | Unauthenticated writes | `app/app.py:100-104` | `PUT /api/items/{id}` needs no token; only `DELETE` checks `X-Admin-Token` |
| 14 | End-of-life runtime | `app/requirements.txt:1-6`, AL2023 `python3` 3.9 | Flask 2.0.3 dates from 2022 and Python 3.9 reached end of life in October 2025 |
| 15 | `pip install` as root into the system Python | `terraform/user_data.sh.tftpl:38` | Shadows the OS's own Jinja2 and MarkupSafe (cloud-init uses them) |
| 16 | HTTP only, open to the world | `app/ops/nginx.conf:2`, `terraform/variables.tf:34` | No TLS anywhere (SEC2) |

## Troubleshooting

- **The app doesn't answer after 10 minutes.** Open a shell over SSM, then run `sudo tail -n 50 /var/log/legacy-bootstrap.log`. A failing step
  logs `FAILED (exit N) at line L`. Re-run the bootstrap with `sudo bash /var/lib/cloud/instance/scripts/part-001`.
- **There's no `CWAgent` memory metric.** Check the bootstrap log for the CloudWatch agent warning; CPU metrics (`AWS/EC2`) still work.
- **A newer AL2023 AMI came out.** The instance ignores AMI changes (`lifecycle.ignore_changes`), so it isn't replaced.
