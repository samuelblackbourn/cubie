PYTHON ?= python3

# Where the office's checkout lives ON OFFICE-SERVER, which is the machine that
# matters -- this used to default to a sandbox path (/workspace/virtual-office),
# so the default was wrong everywhere the bar is actually run. Override it for a
# checkout somewhere else:
#
#   VO_REPO=/path/to/virtual-office make check
#
# The guard refuses a checkout behind its remote (exit 2), so a forgotten clone
# now reports itself instead of passing quietly.
VO_REPO ?= $(HOME)/virtual-office

.PHONY: help check check-contract check-lint check-pa check-idempotent \
        check-gateway check-config gateway-tools pa-install

help:
	@echo "check           the green bar (no CI by design, same as the sibling repos)"
	@echo "check-contract  fail if /api/companion/status has drifted upstream"
	@echo "check-idempotent  prove the firmware patch chain converges (clones, ~minutes)"
	@echo "check-pa        typecheck-free unit tests for the PA service"
	@echo "check-lint      fail on an undefined or unused name in any .py here"
	@echo "check-gateway   fail if the gateway cannot route the tools pa/ calls"
	@echo "check-config    prove the sdkconfig patcher keeps one option per choice"
	@echo "gateway-tools   teach the installed gateway this fleet's display tools"
	@echo "pa-install      create pa/.venv and install requirements"
	@echo ""
	@echo "check-contract needs the upstream shape. Either:"
	@echo "  VO_REPO=/path/to/virtual-office make check-contract"
	@echo "  VO_COMPANION_STATUS=/path/to/companionStatus.ts make check-contract"
	@echo ""
	@echo "Exit 2 means 'could not check' -- NOT 'no drift'. A checkout behind its"
	@echo "own remote is exit 2 too: comparing against stale bytes proves nothing."

# Run this at the START of any session that touches the office's payload, and
# before any deploy. See scripts/check_companion_contract.py for why.
check-contract:
	VO_REPO=$(VO_REPO) $(PYTHON) scripts/check_companion_contract.py

# The contract guard runs FIRST: if the office's payload has drifted, pa/office.py
# is parsing the wrong shape and pa/mood.py is deciding from fields that may no
# longer be there -- so every test after it would be asserting against a fiction.
# The PA service's own venv. Kept separate from the gateway's: the gateway is
# a pinned PyPI release we deliberately do not touch, and installing our
# dependencies into it would be exactly the kind of quiet coupling that makes
# "it is upstream, run it as-is" stop being true.
pa-install:
	$(PYTHON) -m venv pa/.venv
	pa/.venv/bin/pip install --quiet --upgrade pip
	pa/.venv/bin/pip install --quiet -r pa/requirements.txt

# Runs against pa/.venv when it exists, and says how to make one when it does
# not -- rather than falling back to a system python that may or may not have
# httpx and reporting a misleading pass or failure.
check-pa:
	@test -x pa/.venv/bin/pytest || { \
	  echo "pa/.venv not found -- run 'make pa-install' first"; exit 2; }
	pa/.venv/bin/pytest pa/tests -q

# Undefined and unused names, across every Python file in the repo -- not just
# pa/. Deliberately NOT a style check: pyflakes answers one question and has
# nothing to configure, so there is nothing here to argue about or silence.
#
# It earns its place on evidence rather than principle. Two real defects in
# this repo were of exactly this shape: `Pose` and `Any` used in annotations
# that were never imported (harmless under PEP 563, but `get_type_hints`
# raises), and five stale imports that accumulated while it was not a gate.
check-lint:
	@test -x pa/.venv/bin/python || { \
	  echo "pa/.venv not found -- run 'make pa-install' first"; exit 2; }
	@pa/.venv/bin/python -m pyflakes --version >/dev/null 2>&1 || { \
	  echo "pyflakes not installed -- run 'make pa-install' to refresh pa/.venv"; \
	  exit 2; }
	pa/.venv/bin/python -m pyflakes \
	  pa/*.py pa/tests/*.py tools/*.py scripts/*.py firmware/*.py
	@echo "PASS -- no undefined or unused names"

# NOT part of `check`: it needs a gateway installed, which a dev machine has no
# reason to have. Run it on office-server after touching pa/live.py's tool set
# or bumping the gateway pin. It catches the failure that cost a round on
# hardware -- the gateway proxies a HARDCODED tool table, so a tool added to
# the device firmware is unreachable until the gateway knows it too, and an
# unknown tool comes back as a successful-looking {"error": ...} that nothing
# raises on.
#
# Exit 2 means "could not check" -- NOT "nothing missing" -- for the same
# reason check-contract does.
check-gateway:
	$(PYTHON) gateway/check-gateway-tools.py

# Applies the patch, then proves it. Idempotent: re-running prints
# "already patched". A pip --force-reinstall reverts it; re-run this.
gateway-tools:
	bash gateway/apply-gateway-tools.sh
	$(PYTHON) gateway/check-gateway-tools.py

# Part of `check`: it runs in a second, needs no clone and no gateway, and it
# guards a failure that is silent by construction -- two selected options in
# one Kconfig `choice` is an ill-defined build rather than an error.
check-config:
	$(PYTHON) firmware/check-config-choices.py

check: check-contract check-lint check-pa check-config

# NOT part of `check`: it clones two repositories. Run it after touching
# anything in firmware/*.sh. It catches a failure mode that is otherwise
# silent -- a second pass over an already-patched tree producing DIFFERENT
# sources from the first, so incremental builds ship different firmware from
# fresh ones. That happened once and nothing errored; the build just succeeded
# with `surprised` wearing narrower eyes.
check-idempotent:
	bash firmware/check-idempotent.sh
