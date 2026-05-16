"""Manual live WCL fetch helper for one character.

This spends real Warcraft Logs API quota. It is intentionally a manual tool,
not a deterministic test.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from applicant_scout.config import ConfigError, load_config, normalize_wcl_region
from applicant_scout.wcl import CharacterRanks, WCLAuth, WCLClient, derive_server_slug


def _region_arg(value: str) -> str:
    try:
        return normalize_wcl_region(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch WCL raid/M+ data for one character."
    )
    parser.add_argument("name", help="Character name")
    parser.add_argument("realm", help="Realm name, for example Ravencrest")
    parser.add_argument(
        "--region",
        type=_region_arg,
        help="WCL region token (EU, US, KR, TW, CN); defaults to config",
    )
    parser.add_argument("--spec-id", type=int, default=0, help="Applicant spec ID")
    parser.add_argument(
        "--role",
        choices=("DAMAGER", "DPS", "TANK", "HEALER"),
        default="DAMAGER",
        help="Applicant role; controls M+ metric selection",
    )
    return parser.parse_args(argv)


def _print_ranks(client: WCLClient, name: str, realm: str, spec_id: int, role: str) -> None:
    slug = derive_server_slug(realm)
    print(f"\n=== {name} ({realm} -> {slug}) spec={spec_id} role={role} ===")
    ranks = client.fetch_character_ranks(name, slug, spec_id, role)
    _print_result(client, ranks, role)


def _print_result(client: WCLClient, ranks: CharacterRanks, role: str) -> None:
    if ranks.error:
        print(f"  ERROR: {ranks.error}")
        return
    if ranks.not_found:
        print("  NOT FOUND")
        return
    print(
        "  Raid Normal:  "
        f"best={_fmt(ranks.raid_normal)}  median={_fmt(ranks.raid_normal_median)}"
    )
    print(
        "  Raid Heroic:  "
        f"best={_fmt(ranks.raid_heroic)}  median={_fmt(ranks.raid_heroic_median)}"
    )
    print(
        "  Raid Mythic:  "
        f"best={_fmt(ranks.raid_mythic)}  median={_fmt(ranks.raid_mythic_median)}"
    )
    if role == "HEALER":
        print(
            "  M+ HPS Headline:   "
            f"best={_fmt(ranks.mplus_hps)}  median={_fmt(ranks.mplus_hps_median)}"
        )
        _print_breakdown("HPS", ranks.mplus_hps_breakdown)
    else:
        print(
            "  M+ DPS Headline:   "
            f"best={_fmt(ranks.mplus_dps)}  median={_fmt(ranks.mplus_dps_median)}"
        )
        _print_breakdown("DPS", ranks.mplus_dps_breakdown)
    print(f"  Quota: {client.last_quota}")


def _print_breakdown(label: str, breakdown) -> None:
    print(f"  M+ per-dungeon ({label}):")
    for dungeon in breakdown:
        key_level = f"+{dungeon.key_level}" if dungeon.key_level else "?"
        run_count = f"x{dungeon.run_count}" if dungeon.run_count else "-"
        median = (
            f"  median={_fmt(dungeon.median_percent)}"
            if dungeon.run_count >= 2
            else "  median=- (N=1)"
        )
        print(
            f"    {dungeon.name:30s}  {key_level:5s}  {run_count:4s}  "
            f"best={_fmt(dungeon.parse_percent)}{median}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    auth = WCLAuth(cfg.wcl_client_id, cfg.wcl_client_secret, cfg.cache_dir)
    client = WCLClient(auth, region=args.region or cfg.region)
    try:
        _print_ranks(client, args.name, args.realm, args.spec_id, args.role)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
