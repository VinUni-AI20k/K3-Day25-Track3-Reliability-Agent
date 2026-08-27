from __future__ import annotations

import argparse

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    args = parser.parse_args()
    config = load_config(args.config)
    metrics = run_simulation(config, load_queries())
    metrics.write_json(args.out)
    csv_out = args.out.rsplit(".", 1)[0] + ".csv"
    metrics.write_csv(csv_out)
    print(f"wrote {args.out} and {csv_out}")


if __name__ == "__main__":
    main()
