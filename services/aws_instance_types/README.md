# AWS Option Catalogs (EKS Versions, EKS, Aurora, Redis, Kafka)

This service replaces hand-maintained static dropdown lists with AWS API backed catalogs, cached on disk.

## What It Covers

- **EKS cluster versions**
  - Source: `eks:DescribeClusterVersions`
  - Code: `services/aws_instance_types/eks_cluster_versions.py`
  - Used by: `engine/wizards/forms/eks_settings.py`

- **EKS worker node instance types**
  - Source: `ec2:DescribeInstanceTypes`
  - Code: `services/aws_instance_types/eks_instance_types.py`
  - Used by: `engine/wizards/forms/eks_settings.py`

- **Aurora (RDS) instance classes**
  - Source: `rds:DescribeOrderableDBInstanceOptions` (engine-specific)
  - Code: `services/aws_instance_types/aurora_instance_classes.py`
  - Used by: `engine/wizards/forms/aurora_settings.py`

- **ElastiCache (Redis) node types**
  - Source: `elasticache:DescribeReservedCacheNodesOfferings` (extract `CacheNodeType`)
  - Code: `services/aws_instance_types/redis_node_types.py`
  - Used by: `engine/wizards/forms/redis_form.py`

- **Kafka (MSK) versions + broker node instance types**
  - Source:
    - Versions: `kafka:ListKafkaVersions`
    - Broker node instance types: AWS EC2 instance type offerings (region availability filter for supported Kafka types)
  - Code: `services/aws_instance_types/kafka_catalog.py`
  - Used by: `engine/wizards/forms/kafka_form.py`

Each catalog groups values into optgroups (Burstable, General Purpose, etc.) using family-prefix heuristics.

## Caching Model

- Cache files live on disk (default: `/tmp/praxis-cache/`) so multiple gunicorn workers can share results.
- Cache keying:
  - EKS cluster versions: per-region
  - EKS instance types: per-region
  - Redis: per-region
  - Aurora: per-region *and* per-engine
- Each cache file includes `generated_at` and is considered **stale** after the configured TTL (default: 24h).
- Refresh concurrency:
  - Uses a **file lock** (`*.lock`) so only one worker refreshes a given cache at a time.
  - Writes are **atomic** (`os.replace`) to avoid partial JSON reads.

If the cache is missing/invalid, the wizard falls back to the static lists in:
- `engine/wizards/constants/eks_constants.py`
- `engine/wizards/constants/aurora_constants.py`
- `engine/wizards/constants/redis_constants.py`
- `engine/wizards/constants/kafka_constants.py`

## “Do I Have a Visual That It Works?”

Yes:

- There is a dedicated status page at `/status/aws-catalogs` showing:
  - which cache files exist
  - last generated timestamp, stale/TTL, counts
  - a `source` indicator (`aws_cache` vs `fallback`) that matches what the wizards will use
  - group previews
  - refresh buttons + a JSON view per cache
  - region dropdown to focus the page on a specific region
  - async refresh job panel (progress + events) so it doesn't feel like the page is hanging
- The wizard pages for **EKS**, **Aurora**, **Redis**, and **Kafka** show a small badge:
  - `source=aws_cache` means the dropdown is coming from the AWS-cached catalog.
  - `source=fallback` means AWS couldn’t be used yet (no credentials/permissions, API error, or cache not populated).
  - The badge also shows `region`, `count`, and whether it is `stale`.
- Each of those steps includes a **Refresh** button that forces an AWS refresh and reloads the page.

Manual refresh query params:
- EKS cluster versions: `?eks_cluster_versions_refresh=1`
- EKS: `?eks_instance_types_refresh=1`
- Aurora: `?aurora_instance_classes_refresh=1`
- Redis: `?redis_node_types_refresh=1`
- Kafka: `?kafka_catalog_refresh=1`

The refresh handling lives in `engine/wizards/factory/views.py`.

## Background Refresh (Periodic)

On app startup, `app.py` starts a lightweight background thread:
- `services/aws_instance_types/refresher.py:start_aws_catalog_refresher()`

It wakes periodically and refreshes any stale caches (under the same file lock).

Important: **refresh interval** vs **cache TTL**
- `PS_AWS_CATALOG_REFRESH_INTERVAL_SECONDS` controls how often we *check* (default: **3600s**).
- The per-catalog TTL controls how often we *actually refresh* (default: **86400s = 24h**).
- If you want a true "refresh about once per hour", set TTLs to `3600` (and keep the interval at `3600`).

## CLI Refresh (Manual Trigger)

From the repo/container:

```bash
# refresh a single catalog
python3 -m services.aws_instance_types.refresh --catalog eks_versions --region us-east-1
python3 -m services.aws_instance_types.refresh --catalog eks         --region us-east-1
python3 -m services.aws_instance_types.refresh --catalog aurora --region us-east-1 --engine aurora-postgresql
python3 -m services.aws_instance_types.refresh --catalog redis  --region us-east-1

# refresh all catalogs
python3 -m services.aws_instance_types.refresh --catalog all --region us-east-1
```

Exit code is `0` on success; `2` if refresh failed.

## Required AWS Permissions

The AWS identity used by the app must allow:

- `eks:DescribeClusterVersions`
- `ec2:DescribeInstanceTypes`
- `rds:DescribeOrderableDBInstanceOptions`
- `elasticache:DescribeReservedCacheNodesOfferings`
- `kafka:ListKafkaVersions`

If these permissions are missing, the UI will still work using the static fallback lists.

Note: `elasticache:DescribeCacheNodeTypeOfferings` is **not** a valid IAM action; the implementation uses
`DescribeReservedCacheNodesOfferings` and extracts `CacheNodeType`.

Example IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PraxisStudioAwsCatalogRead",
      "Effect": "Allow",
      "Action": [
        "eks:DescribeClusterVersions",
        "ec2:DescribeInstanceTypes",
        "rds:DescribeOrderableDBInstanceOptions",
        "elasticache:DescribeReservedCacheNodesOfferings",
        "kafka:ListKafkaVersions"
      ],
      "Resource": "*"
    }
  ]
}
```

## Configuration

All TTLs default to 24 hours.

Region selection / "where do we refresh?":
- Wizards prefer the region selected in the wizard **Common** step (saved in session). If absent, they fall back to `AWS_DEFAULT_REGION` (or `us-east-1`).
- The periodic refresher and `/status/aws-catalogs` "Configured Regions" use `PS_AWS_CATALOG_REFRESH_REGIONS` (comma-separated).
  - If not set, it falls back to `AWS_DEFAULT_REGION`.
  - This lets you keep `AWS_DEFAULT_REGION` aligned to "where the app lives" (e.g. S3 release bucket), while refreshing catalogs in the region you build environments in most (e.g. `us-east-1`).

EKS:
- `PS_EKS_CLUSTER_VERSIONS_TTL_SECONDS`
- `PS_EKS_CLUSTER_VERSIONS_CACHE_DIR`
- `PS_EKS_INSTANCE_TYPES_TTL_SECONDS`
- `PS_EKS_INSTANCE_TYPES_CACHE_DIR`

Aurora:
- `PS_AURORA_INSTANCE_CLASSES_TTL_SECONDS`
- `PS_AURORA_INSTANCE_CLASSES_CACHE_DIR`
- `PS_AURORA_ENGINE` (default engine used for lookups when not available from the wizard/session; default `aurora-postgresql`)

Redis:
- `PS_REDIS_NODE_TYPES_TTL_SECONDS`
- `PS_REDIS_NODE_TYPES_CACHE_DIR`

Kafka:
- `PS_KAFKA_CATALOG_TTL_SECONDS`
- `PS_KAFKA_CATALOG_CACHE_DIR`

Periodic refresher:
- `PS_AWS_CATALOG_REFRESHER_ENABLED` (default `1`)
  - Backwards compatible: `PS_EKS_INSTANCE_TYPES_REFRESHER_ENABLED`
- `PS_AWS_CATALOG_REFRESH_INTERVAL_SECONDS` (default `3600`)
  - Backwards compatible: `PS_EKS_INSTANCE_TYPES_REFRESH_INTERVAL_SECONDS`
- `PS_AWS_CATALOG_REFRESH_REGIONS` (comma-separated; defaults to `AWS_DEFAULT_REGION`)
  - Backwards compatible: `PS_EKS_INSTANCE_TYPES_REFRESH_REGIONS`
