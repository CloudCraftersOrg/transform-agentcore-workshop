"""Renders reports/finops-report.md: deterministic tables plus the narrative the LLM wrote."""

from typing import Any

from . import cost_model as cm

WHAT_YOU_PAY_FOR = {
    "control-plane": "Managed Kubernetes API and etcd across 3 AZs, patched by AWS. Fixed per cluster, not per load.",
    "compute": "The nodes that run the pods: 2+ nodes in different AZs, so losing a node or an AZ keeps the API up.",
    "database": "Managed PostgreSQL with a synchronous standby in a second AZ, automated backups and point-in-time "
                "recovery (RPO 15 min, RTO 1 h) instead of a SQLite file on one disk.",
    "network": "Load balancer with health checks across AZs, NAT egress for private nodes, public IPv4 addresses "
               "and internet egress.",
    "storage": "Node root volumes and the private container registry.",
    "observability": "Central JSON logs (NFR5) instead of log files on one server's disk.",
    "security": "Database credentials in Secrets Manager instead of in the code (SEC1).",
}
CATEGORIES = list(WHAT_YOU_PAY_FOR)


def _usd(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def _by_category(items: list[dict]) -> dict[str, float]:
    out: dict[str, int] = {}
    for i in items:
        out[i["category"]] = out.get(i["category"], 0) + round(i["monthly_usd"] * 100)
    return {k: v / 100 for k, v in out.items()}


def _items_table(items: list[dict]) -> list[str]:
    rows = ["| Line item | USD/month | Formula |", "|---|---:|---|"]
    rows += [f"| {i['item']} | {_usd(i['monthly_usd'])} | `{i['calc']}` |" for i in items]
    rows.append(f"| **Total** | **{_usd(cm.total(items))}** | |")
    return rows


def render(data: dict[str, Any], narrative: dict[str, str], service: str, as_of: str) -> str:
    cur, des, opt = data["current"], data["target_as_designed"], data["target_optimized"]
    budget = data["budget_usd"]
    ok = lambda total: "yes" if total <= budget else "**no**"
    lines = [
        f"# FinOps report: {service}",
        "",
        f"Pricing: `{data['pricing_source']}` (us-west-2, {data['currency']}, 730 h/month) · analysis date {as_of}",
        "",
        "## Summary",
        "",
        narrative.get("summary", "").strip(),
        "",
        *_unverified_note(data.get("unverified_numbers")),
        *([f"> {data['eks_support_alert']}", ""] if data.get("eks_support_alert") else []),
        f"| Scenario | USD/month | vs current | Within USD {budget:,.0f} budget |",
        "|---|---:|---:|:---:|",
        (f"| Current: EC2 {data['assumptions']['current_instance']} monolith | {_usd(cur['monthly_usd'])} | - | "
        f"{ok(cur['monthly_usd'])} |"),
        (f"| Target as designed (terraform + k8s) | {_usd(des['monthly_usd'])} | "
        f"{data['deltas']['as_designed_vs_current_usd']:+,.2f} | {ok(des['monthly_usd'])} |"),
        (f"| **Target optimized (recommended)** | **{_usd(opt['monthly_usd'])}** | "
        f"{data['deltas']['optimized_vs_current_usd']:+,.2f} | {ok(opt['monthly_usd'])} |"),
        "",
        "## Why the target costs more, and what it buys",
        "",
        narrative.get("value_case", "").strip(),
        "",
        "## What you pay for",
        "",
        "| Category | Current | As designed | Optimized | What it buys |",
        "|---|---:|---:|---:|---|",
    ]
    cats = [_by_category(s["items"]) for s in (cur, des, opt)]
    for cat in CATEGORIES:
        if any(cat in c for c in cats):
            cells = " | ".join(_usd(c.get(cat, 0.0)) for c in cats)
            lines.append(f"| {cat} | {cells} | {WHAT_YOU_PAY_FOR[cat]} |")
    lines.append(f"| **Total** | **{_usd(cur['monthly_usd'])}** | **{_usd(des['monthly_usd'])}** | "
                 f"**{_usd(opt['monthly_usd'])}** | |")

    lines += ["", "## Recommendations", "",
              "| ID | Recommendation | Saves USD/month | Effort | Risk | Applies to | Why |",
              "|---|---|---:|---|---|---|---|"]
    for r in data["recommendations"]:
        why = (r.get("rationale") or r.get("change") or "").replace("|", "/").replace("\n", " ")
        saves = _usd(r["monthly_savings_usd"]) if r.get("quantified", True) else "not quantified"
        lines.append(f"| {r['id']} | {r['title']} | {saves} | {r['effort']} | {r['risk']} | "
                     f"{r['applies_to']} | {why} |")
    if data.get("required_fixes"):
        lines += ["", "**Required fixes** (spec or SLO gaps in the design; applied to the optimized scenario):", ""]
        lines += [f"- `{f['id']}`: {f['reason']} ({f['monthly_delta_usd']:+,.2f} USD/month)"
                  for f in data["required_fixes"]]
    if data.get("rejected"):
        lines += ["", "**Not applied to production** (SLO guardrails):", ""]
        lines += [f"- `{r['id']}`: {r['reason']}" for r in data["rejected"]]
    lines += ["", "**Dev/test environments** (not in the production totals):", ""]
    lines += [f"- {n['title']}: saves {_usd(n['monthly_savings_usd'])} USD/month per environment (`{n['calc']}`)"
              for n in data["nonprod"]]

    rs = data["rightsizing"]
    declared = data.get("declared_resources") or {}
    mem = data.get("memory_sizing") or {}
    vm_mem = mem.get("vm_estimate") or {}
    vm_req, vm_lim = (vm_mem.get(k, {}).get("memory", "-") for k in ("requests", "limits"))
    measured = f"{mem['measured_mi']:g}Mi working set" if mem.get("measured_mi") else "-"
    lines += ["", "## Right-sizing", "",
              "| | Declared (k8s manifests) | Measured (container) | Legacy-VM estimate (upper bound) | Recommended |",
              "|---|---|---|---|---|",
              (f"| Pod requests | {_res(declared.get('requests'))} | {measured} | {vm_req} memory | "
               f"{_res(rs['pod_requests'])} |"),
              f"| Pod limits | {_res(declared.get('limits'))} | - | {vm_lim} memory | {_res(rs['pod_limits'])} |",
              f"| Replicas (HPA) | {declared.get('replicas', '-')} | - | - | {rs['replicas_min']}-{rs['replicas_max']} |",
              (f"| Nodes | {data['assumptions']['designed_nodes']} | - | - | {rs['node_min']}-{rs['node_max']} x "
              f"{', '.join(rs['node_instance_types'])} |"),
              f"| Database | {des['design']['db_class']} | - | - | {rs['db_instance_class']} |", ""]
    if mem.get("gap_note"):
        lines += [mem["gap_note"], ""]
    lines += ["How the pod size was derived (CPU from the legacy VM's p95, memory from the container):", ""]
    lines += [f"- `{f}`" for f in data.get("rightsizing_formula", [])]
    growth = data.get("growth") or {}
    if growth:
        lines += ["", (f"Growth check: at {data['assumptions']['growth_factor']:g}x traffic the HPA needs "
                      f"{growth['replicas']} replicas on {growth['nodes']} nodes: {_usd(growth['monthly_usd'])} "
                      f"USD/month.")]

    lines += ["", "### Node fit (cheapest first)", "",
              "| Instance | vCPU | GiB | Nodes | Pods/node allocatable (CPU, memory) | USD/month (nodes + volumes) |",
              "|---|---:|---:|---:|---|---:|"]
    for r in [r for r in data.get("node_fit", []) if r.get("fits")][:6]:
        a = r["allocatable"]
        lines.append(f"| {r['instance_type']} | {r['vcpu']} | {r['memory_gib']:g} | {r['nodes']} | "
                     f"{a['cpu_m']}m, {a['memory_mi']}Mi | {_usd(r['monthly_usd'])} |")

    lines += ["", "## Line items", "", "### Current", "", *_items_table(cur["items"]),
              "", "### Target as designed", "", *_items_table(des["items"]),
              "", "### Target optimized", "", *_items_table(opt["items"])]

    lines += ["", "## Assumptions", ""]
    lines += [f"- {a}" for a in data["assumptions"]["notes"]]
    if narrative.get("risks"):
        lines += ["", "## Risks and follow-ups", "", narrative["risks"].strip()]
    return "\n".join(lines) + "\n"


def _unverified_note(numbers: list[str] | None) -> list[str]:
    if not numbers:
        return []
    note = f"> Note: the narrative quotes numbers the cost model did not produce: {', '.join(numbers)}."
    return [note + " Trust the tables.", ""]


def _res(res: dict | None) -> str:
    return f"{res['cpu']} CPU, {res['memory']}" if res else "-"
