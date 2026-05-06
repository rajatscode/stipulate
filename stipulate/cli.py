from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(prog="stipulate")
    parser.add_argument(
        "command",
        choices=["explore"],
        help="Run direct-mode exploration from Python integration code.",
    )
    parser.parse_args()
    print("stipulate explore is available through stipulate.Explorer in this vertical slice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
