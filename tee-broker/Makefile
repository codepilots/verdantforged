# VerdantForged TEE Broker — Makefile
#
# Targets:
#   test              Run all non-live tests (default `pytest` collection)
#   regression-live   Run only live tests (requires AWS credentials)
#   regression-cfn    Run the CFN NoEcho live regression specifically
#   lint              Run cfn-lint on the CFN templates
#
# Live tests are skipped by default. Use `make regression-live` or
# `make regression-cfn` to run them. See tests/README-live-regression.md.

PYTHON ?= python3
PIP ?= pip

.PHONY: test test-file-lifecycle regression-live regression-cfn lint help

help:
	@echo "Targets:"
	@echo "  test              Run the maintained non-live contract suites"
	@echo "  test-file-lifecycle Run encrypted file-job TDD suite"
	@echo "  regression-live   Run ALL live tests (requires AWS creds)"
	@echo "  regression-cfn    Run CFN NoEcho live regression only"
	@echo "  lint              Run cfn-lint on CFN templates"

test:
	$(PYTHON) tests/verify-file-lifecycle.py
	$(PYTHON) tests/verify-input-attachments.py
	$(PYTHON) tests/verify-artifacts-s3.py
	$(PYTHON) tests/verify-sandbox-execution.py
	$(PYTHON) tests/verify-tool-calling-loop.py

test-file-lifecycle:
	$(PYTHON) tests/verify-file-lifecycle.py

regression-live:
	$(PYTHON) -m pytest -v -m live_cfn

regression-cfn:
	$(PYTHON) -m pytest -v -m live_cfn tests/verify-cfn-noecho-live.py

lint:
	cfn-lint cloudformation-control-plane.yaml
	cfn-lint cloudformation-worker.yaml
