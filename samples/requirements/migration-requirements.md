# Migration Request — Legacy Inventory & Orders

**Requested by:** Operations IT · **Business owner:** Supply Chain · **Date:** 2026-10-01

## 1. Background

The Inventory & Orders application runs as a single Flask process on one EC2 instance
(`legacy-inventory`, us-west-2). It was deployed by hand in 2017. Nobody wants to touch the
server, there is no staging environment, and the nightly CSV report sometimes fails when the disk fills up.
Last quarter's audit flagged hard-coded credentials and an injectable search endpoint.

## 2. Business goals

- G1. Move the application to our standard container platform (**Amazon EKS**) as an independently deployable **microservice**.
- G2. Remove the single point of failure: survive the loss of one node or one Availability Zone.
- G3. Cut infrastructure cost, or at minimum not increase it. Finance has approved a **maximum of USD 450 per month** for production.
- G4. Be in production before the end of Q1 2027.

## 3. Functional requirements

- FR1. Keep the existing JSON API contract (`/api/items`, `/api/items/{id}`, `/api/orders`) so the warehouse scanners keep working without changes.
- FR2. The HTML dashboard can stay, but it is low priority. API consumers come first.
- FR3. The nightly inventory report must keep running, but it must not depend on the local disk of a single machine.
- FR4. Order creation must never oversell stock (current behaviour must be preserved).

## 4. Non-functional requirements

- NFR1. Availability SLO **99.9%** monthly for the API.
- NFR2. p95 latency **< 300 ms** at the expected peak of **40 requests/second** (warehouse shift change, 06:00–07:00).
- NFR3. Scale out automatically on CPU; at least **2 replicas** at all times in production.
- NFR4. RPO 15 minutes, RTO 1 hour.
- NFR5. Structured (JSON) logs to stdout; no log files on disk.
- NFR6. Health and readiness endpoints for the orchestrator.

## 5. Security & compliance

- SEC1. No credentials in code or container images; secrets come from AWS Secrets Manager.
- SEC2. Data encrypted at rest and in transit inside the VPC.
- SEC3. The database must not be reachable from the internet.
- SEC4. Containers run as non-root with a read-only root filesystem where possible.
- SEC5. Fix the audit finding on the search endpoint (SQL injection).

## 6. Technical constraints & preferences

- TC1. Region **us-west-2**.
- TC2. Prefer **Graviton (arm64)** instances for cost.
- TC3. Target database: **Amazon RDS for PostgreSQL** (the team does not want to operate a database on Kubernetes).
- TC4. Infrastructure as code with **Terraform**; container images in **Amazon ECR**.
- TC5. Kubernetes manifests must pass our policy baseline: resource requests/limits, probes, non-root, no `latest` tags, PodDisruptionBudget.

## 7. Open points (to be confirmed)

- The warehouse team mentioned "maybe 3x more traffic next year". It is unclear whether we should size for that now.
- Dev/test environments: "something cheap" was requested, but there is no budget line for them yet.
- Data migration window: the business prefers a weekend cutover, but no date is set.
