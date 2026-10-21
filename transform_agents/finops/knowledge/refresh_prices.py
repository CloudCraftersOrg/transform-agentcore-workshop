"""Refresh the committed price snapshot from the live AWS APIs (read-only calls only).

    AWS_PROFILE=<profile> uv run python transform_agents/finops/knowledge/refresh_prices.py

Uses the Pricing API (us-east-1) for US West (Oregon), the Savings Plans offering rates API,
EC2 DescribeInstanceTypes / DescribeSpotPriceHistory and EKS DescribeClusterVersions.
"""

import json
import time

from transform_agents.finops.pricing import SNAPSHOT_PATH, fetch_live_prices


def main() -> None:
    started = time.time()
    prices = fetch_live_prices()
    SNAPSHOT_PATH.write_text(json.dumps(prices, indent=2) + "\n")
    print(f"wrote {SNAPSHOT_PATH} (fetched_at={prices['fetched_at']}, {time.time() - started:.1f}s)")


if __name__ == "__main__":
    main()
