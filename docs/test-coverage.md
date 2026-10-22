# Test coverage

This report records the verified Day 6 test snapshot. It is evidence for that
commit, not a permanent claim about later revisions.

## Verified snapshot

| Measure | Result |
| --- | ---: |
| Tests | 248 passed |
| Statements | 3,582 / 4,026 (88.97%) |
| Branches | 505 / 688 (73.40%) |
| Pytest duration | 402.40 seconds |
| CI run | Passed |

The CI run is available in
[GitHub Actions](https://github.com/itsdevcode/OrderFlow/actions/runs/39825899644).
The terminal summary rounds combined coverage to 87%; the percentages above come
from coverage JSON so line and branch measurements remain distinct.

## High-risk service coverage

| Module | Lines | Branches |
| --- | ---: | ---: |
| Authentication service | 88.60% | 69.44% |
| Authentication dependency | 96.00% | 83.33% |
| Order service | 98.89% | 90.00% |
| Inventory service | 88.33% | 68.75% |
| Reservation service | 95.35% | 81.25% |
| Payment service | 85.50% | 70.45% |
| Webhook service | 86.96% | 71.88% |
| Audit service | 100.00% | 100.00% |
| Rate-limit service | 97.67% | 83.33% |
| Shipment service | 84.05% | 74.19% |
| Notification service | 78.08% | 50.00% |

## Confidence checks

- Four concurrency-sensitive tests were repeated five times each: 20 of 20 runs
  passed.
- Reverting the refresh-rotation fix caused its race test to fail.
- Disabling expiration verification caused both expired-token tests to fail.
- CI type-checked `app`, exercised the latest migration downgrade/upgrade path,
  ran branch coverage, and smoke-tested the built container.

These mutation checks show that targeted tests detect the regressions they were
added to prevent. They do not represent general mutation coverage.

## Running the checks

With PostgreSQL and Redis available and the documented test environment exported:

```bash
venv/bin/pyright app
venv/bin/alembic upgrade head
venv/bin/pytest --cov=app --cov-branch --cov-report=term-missing
```

The full environment and Docker commands are in [Operations](operations.md).

## Known gaps

- Notification delivery has the lowest branch coverage among listed services.
- The project does not enforce a numeric coverage threshold in CI.
- CI tests the latest migration's downgrade/upgrade path; it does not prove every
  historical migration or compare all ORM metadata with the live schema.
- Coverage cannot establish correctness for provider outages, multi-instance
  timing, PostgreSQL failover, or real Redis network partitions by itself.
- There is no trustworthy before/after comparison because the earlier run used a
  different concurrency tracing configuration.
