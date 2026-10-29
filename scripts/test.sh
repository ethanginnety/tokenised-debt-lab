#!/usr/bin/env bash
# Run every test and write a JUnit report to test-results.xml.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest -v --junitxml=test-results.xml "$@"
