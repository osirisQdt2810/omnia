# CI / test image for Omnia — runs the test suite and linters in a pinned, reproducible
# environment. This image NEVER runs the add-on itself (the add-on runs inside the user's
# Anki). Its only job is `pytest` + pre-commit-style checks on a fixed Python version that
# matches Anki's bundled interpreter (latest Anki ships CPython 3.13), for cross-platform sanity.
#
#   docker build -f deploy/Dockerfile -t omnia-ci .
#   docker run --rm omnia-ci
FROM python:3.13-slim

WORKDIR /app

COPY requirements/requirements-dev.txt requirements/requirements-dev.txt
RUN pip install --no-cache-dir -r requirements/requirements-dev.txt

COPY . .

# Lint the add-on source (not vendor/ or references/), then run the logic tests.
CMD ["sh", "-c", "ruff check src/omnia && black --check src/omnia tests && isort --check-only src/omnia tests && pytest tests/ -vv"]
