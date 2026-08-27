from __future__ import annotations

import argparse
import random
from pathlib import Path

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument("--csv", default=None, help="CSV path (default: --out with .csv suffix)")
    parser.add_argument("--seed", type=int, default=None, help="seed the RNG so a run reproduces")
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    config = load_config(args.config)
    metrics = run_simulation(config, load_queries())
    metrics.write_json(args.out)
    csv_path = args.csv or str(Path(args.out).with_suffix(".csv"))
    metrics.write_csv(csv_path)
    print(f"wrote {args.out}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
