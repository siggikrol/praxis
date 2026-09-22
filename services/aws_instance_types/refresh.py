from __future__ import annotations

import argparse
import json
import os

from .registry import BY_KEY, iam_policy, refresh_region, requested_catalogs


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh AWS option catalogs")
    parser.add_argument('--catalog', choices=[*BY_KEY, 'kafka', 'all'], default='eks')
    parser.add_argument('--region', default=os.getenv('AWS_DEFAULT_REGION', 'us-east-1'))
    parser.add_argument('--engine', default='', help='Engine/version variant for a single catalog')
    parser.add_argument('--iam-policy', action='store_true', help='Print required read-only IAM policy without calling AWS')
    args = parser.parse_args(argv)
    if args.iam_policy:
        print(json.dumps(iam_policy(), indent=2))
        return 0
    results = refresh_region(args.region, force=True, catalogs=requested_catalogs(args.catalog),
                             engine=args.engine if args.catalog != 'all' else '')
    output = next(iter(results.values())) if args.catalog not in ("all", "kafka") and len(results) == 1 else results
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if all(result.get('ok') for result in results.values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
