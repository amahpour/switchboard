### Added

- **Security reports.** Pull requests and main now get downloadable CodeQL, Bandit, dependency, image, workflow and secret-scan reports. Findings are report-only; `uv run python scripts/security_scan.py` runs the fast checks locally before a pull request.
