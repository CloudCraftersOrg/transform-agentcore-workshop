# Demo content spec

> Last verified: 2026-10-20 · Scope: everything the talk depends on that isn't code behaviour: the sample inputs, the
> legacy smells, the planted finding, the recorded run, the script and its numbers, the on-screen narrative text, and the
> facts that expire with time.

## 1. Purpose and scope

The demo is only convincing if what the presenter **says**, what the screen **shows** and what the recording **replays**
agree. This spec lists those assets, the rules that keep them consistent, and a calendar of the facts that go stale.

## 2. Invariants

1. **Script = screen = recording.** Every number in [docs/KEYNOTE-SCRIPT.md](../KEYNOTE-SCRIPT.md) (the cheat sheet and
   spoken lines) and in the [WORKSHOP.md](../../WORKSHOP.md) §5 reference table MUST match `make numbers` run on
   [samples/recorded-run/](../../samples/recorded-run/), rounded with "~" where the text says so.
2. **The recording MUST be a complete run:** all six stages approved, the Requirements revision included (attempt 2),
   `simulate_drift` and `live_deploy` on, and no personal identifiers. `orchestrator.record` refuses incomplete runs and
   redacts paths.
3. **Honesty:** the UI and the script MUST say the demo is a re-creation of the loop AWS Transform's agents run, not AWS
   Transform itself. Simulated drift MUST be labelled as simulated. A replay MUST show the "Recorded Run" chip.
4. **No unverifiable claims about AWS Transform:** no performance statistics unless they're quoted from an official AWS
   source on the slide. Capability lists MUST be re-checked against the AWS Transform pages before each talk.
5. **The sample requirements are a contract with the tests.**
   [migration-requirements.md](../../samples/requirements/migration-requirements.md) MUST keep yielding: budget 450,
   peak 40 rps, p95 300 ms, SLO 99.9, replicas min 2 / max 6, us-west-2, arm64, PostgreSQL on RDS, Multi-AZ and Secrets
   Manager (asserted by the requirements live test), plus three open points (growth, dev/test, cutover).
6. **The legacy smells are deliberate.** [legacy-infra/app/](../../legacy-infra/app/) MUST keep every smell in the
   [smells table](../../legacy-infra/README.md#deliberate-legacy-smells), with correct file:line references. Never "fix"
   the legacy app.
7. **The planted finding** (section 4) MUST stay valid on the day of the talk, or the storyline MUST be moved as
   described below.

## 3. Asset inventory

| Asset | Where | Drives |
|---|---|---|
| Sample requirements | [samples/requirements/migration-requirements.md](../../samples/requirements/migration-requirements.md) | Requirements stage, budget, SLO, open questions |
| Scripted revision | KEYNOTE Module 3: *"Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."* | Resolves Q-01 (growth) and Q-02 (dev/test); Q-03 (cutover) stays open |
| Legacy app and smells | [legacy-infra/app/](../../legacy-infra/app/), [legacy-infra/README.md](../../legacy-infra/README.md) | Discovery findings (17 rules LF-01…LF-17), modernization fixes |
| Drift injector | `transform_agents/k8s_validation/drift.py` | Four simulated violations (POL-001, POL-005, POL-009, POL-011) fixed live |
| Price snapshot | `transform_agents/finops/knowledge/prices-us-west-2.json` | Every FinOps number, and the EKS support table |
| Narrative text on screen | `ui/style.py` `STAGE_META` (`doing`, `bridge`, `benefit`) | The "In AWS Transform / Benefit" strips and live status lines |
| Recording | [samples/recorded-run/](../../samples/recorded-run/) | Plan B replay, the UI tests that read it |
| Fixtures | `tests/fixtures/*_result.json` | Unit tests; the last-resort replay source |
| Script and cheat sheet | [docs/KEYNOTE-SCRIPT.md](../KEYNOTE-SCRIPT.md) | What the presenter says |
| Runbook timings | [WORKSHOP.md](../../WORKSHOP.md) §5 | Rehearsal expectations |

Current numbers (`make numbers`, recording of 2026-10-20):

| Stage | Time | Numbers |
|---|---|---|
| Discovery | 9.8 s | 17 findings (3 critical), readiness 50/100 (varies 35–50 between runs), replatform |
| Requirements | 18.1 s (revision 11.2 s) | 24 requirements (13 traced), budget $450, 3 open questions, 2 resolved |
| Modernization | 57.0 s | 23 tests passed, 32 files |
| Terraform | 17.1 s | valid (full `terraform validate`), 17/17 spec checks, Kubernetes 1.33 |
| K8s Validation | 15.8 s | 4/4 drift fixed, live deploy passed, smoke 200/200/200/201/409 |
| FinOps | 10.9 s | $72 today → $623 as designed → $208 optimized, within the $450 budget |
| Whole pipeline | ~122 s | ~251k tokens |

## 4. The planted finding (Kubernetes 1.33)

- **What happens:** the spec template defaults to Kubernetes **1.33** (`requirements/tools.py`), and so does the
  Terraform blueprint (`blueprint/variables.tf`). FinOps checks the version against the EKS support table in the price
  snapshot. 1.33 left standard support on **2026-07-29**, so the cluster costs an extra $0.50/h, which is **+$365/month**.
  The as-designed total goes over budget ($623). Recommendation FO-01 upgrades to the snapshot's `default_version`
  (**1.36**), and the optimized total ($208) fits the budget.
- **Why it's kept:** it's the talk's "aha" moment. The FinOps agent catches what the spec and the IaC agent missed.
- **How it expires:**
  - **2027-07-29:** 1.33 leaves extended support. EKS won't create 1.33 clusters after that, so the storyline breaks.
    Before that date, move the planted version to one in extended support at the talk date (1.34 is in extended support
    from 2026-12-02 to 2027-12-02).
  - **2027-08-02:** the recommended target 1.36 leaves standard support. Bump `default_version` in the snapshot (by
    refreshing prices) and check FO-01's wording.
  - **Detection:** `eks_support()` computes the status for the run date. After 2027-07-29 it reports 1.33 as
    `END_OF_SUPPORT`, so the FinOps text would no longer match the script.
- **Moving the storyline** (one change, then re-record):
  1. Set the new version in `requirements/tools.py` (spec template) and the blueprint's `kubernetes_version` default.
  2. Refresh the price snapshot so the support table is current.
  3. Search the script, WORKSHOP.md, GUIDELINES examples and the alert copy for "1.33" and "1.36".
  4. Re-run the full pipeline, `make record`, `make numbers`, then update the cheat sheet.

## 5. Expiry calendar

Check this table before every talk. Dates are the facts recorded in the repo on 2026-10-20.

| Fact | Where | Expires or changes | Action |
|---|---|---|---|
| EKS 1.33 extended support | price JSON `eks.versions` | 2027-07-29 | Move the planted version (section 4) |
| EKS 1.36 standard support | price JSON | 2027-08-02 | Refresh prices; new `default_version` |
| EKS 1.34 standard support | price JSON | 2026-12-02 | Becomes the candidate planted version |
| AWS prices | `prices-us-west-2.json` (`fetched_at` 2026-10-19) | Quarterly, or when AWS changes prices | `refresh_prices.py`, then update the finops tests' pinned AS_OF and the numbers |
| Terraform module versions | `terraform/knowledge/module-inputs.json` (eks 21.29.0, vpc 6.7.3, aws 6.69.1, generated 2026-10-19) | On a new major version or a breaking change | `extract_module_inputs.py`, then the blueprint and the live test |
| Model ids and availability | `core/config.py` (benchmark 2026-10-19: Nova 2 Lite, DeepSeek V3.2) | When Bedrock retires or adds models | `make preflight`; re-benchmark ([02-agents.md](02-agents.md)) |
| "Latest version" table for the code scan | `discovery/codescan.py` `LATEST` (curated 2026-10) | Every few months | Update the versions; the "outdated framework" finding depends on it |
| Legacy Python 3.9 end-of-life note | `discovery/infra.py` | Stays true (EOL 2025-10) | None |
| AgentCore CLI 0.31.1 and CDK alpha | `deploy/.cli`, `deploy/agentcore/cdk` | Frequently | Upgrade deliberately ([04-operations.md](04-operations.md)) |
| kind node image v1.36.4 | `scripts/kind-config.yaml` | When 1.36 is old | Bump kind and the node image together |
| AWS Transform capabilities in the script | KEYNOTE Part 1, "Real AWS Transform" lines, `STAGE_META.bridge` | Any AWS Transform launch | Re-verify on the official pages; edit the text |
| The recording | `samples/recorded-run` | After any change that alters agent output, numbers or the UI flow | Re-record (section 6) |

## 6. Change recipes

- **Re-record the fallback** after an agent, model, price or blueprint change, or once the legacy EC2 is up (to capture
  live CloudWatch data):
  ```bash
  make preflight
  uv run python -m transform_agents.orchestrator --requirements samples/requirements/migration-requirements.md \
    --mode local --auto-approve --live-deploy --simulate-drift [--live-aws] \
    --revise requirements="Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."
  make record RUN=runs/<id>
  make numbers
  make replay REPLAY_SPEED=4
  ```
  Then update the cheat sheet and WORKSHOP §5 from `make numbers`, and commit the recording together with the doc
  changes. Some UI tests read the recording; run `uv run pytest -q`.
- **Change the sample requirements:**
  1. Edit the document.
  2. Update the expected values asserted in `tests/test_requirements.py` (live test) and in invariant 5.
  3. Check that the scripted revision still resolves the questions it should.
  4. Re-record.
- **Change the script:** keep spoken numbers in sync with `make numbers`; keep UI labels in Title Case exactly as on
  screen ([GUIDELINES.md](../design/GUIDELINES.md)); keep the "not AWS Transform" line.
- **Change the on-screen narrative** (`STAGE_META.bridge` / `benefit` / `doing`): keep it consistent with the script's
  "Real AWS Transform" lines and in sentence case.
- **Add or alter a legacy smell:**
  1. Change the legacy app.
  2. Update the smells table (file:line).
  3. Update discovery's `assessment_rules.yaml` if a new rule is needed.
  4. Check the modernization tests that cover the fix.
  5. Re-record.

## 7. Verification

- `make numbers` agrees with the cheat sheet and WORKSHOP §5.
- `make replay REPLAY_SPEED=4` plays six stages plus the revision, and the "Recorded Run" chip shows in the UI.
- A full rehearsal in the UI in presenter mode at the venue's resolution, in both themes if you plan to switch.
- `grep -rn "1\.33\|1\.36" docs WORKSHOP.md transform_agents/requirements transform_agents/terraform/knowledge` shows
  one consistent storyline.

## 8. Known debt

- **The recording was made with Live AWS Discovery off**, so Discovery shows CPU as an estimate. Re-record once the
  legacy EC2 is deployed.
- **Results depend on the date:** FinOps uses `date.today()` against the snapshot's support dates, so the same inputs
  can give different results on a later date. The finops unit tests pin AS_OF to 2026-10-19 for that reason.
