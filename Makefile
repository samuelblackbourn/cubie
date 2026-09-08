PYTHON ?= python3
VO_REPO ?= /workspace/virtual-office

.PHONY: help check check-contract check-bridge check-pa check-idempotent \
        check-gateway gateway-tools bridge-install pa-install

help:
	@echo "check           the green bar (no CI by design, same as the sibling repos)"
	@echo "check-contract  fail if /api/companion/status has drifted upstream"
	@echo "check-bridge    typecheck + test the bridge"
	@echo "check-idempotent  prove the firmware patch chain converges (clones, ~minutes)"
	@echo "check-pa        typecheck-free unit tests for the PA service"
	@echo "check-gateway   fail if the gateway cannot route the tools pa/ calls"
	@echo "gateway-tools   teach the installed gateway this fleet's display tools"
	@echo "bridge-install  npm install in bridge/"
	@echo "pa-install      create pa/.venv and install requirements"
	@echo ""
	@echo "check-contract needs the upstream shape. Either:"
	@echo "  VO_REPO=/path/to/virtual-office make check-contract"
	@echo "  VO_COMPANION_STATUS=/path/to/companionStatus.ts make check-contract"
	@echo ""
	@echo "Exit 2 means 'could not check' -- NOT 'no drift'. A checkout behind its"
	@echo "own remote is exit 2 too: comparing against stale bytes proves nothing."

# Run this at the START of any session that touches the bridge, and before any
# deploy. See scripts/check_companion_contract.py for why.
check-contract:
	VO_REPO=$(VO_REPO) $(PYTHON) scripts/check_companion_contract.py

bridge-install:
	cd bridge && npm install --no-audit --no-fund

# Typecheck and unit tests. The interesting behaviour lives in
# bridge/src/posture.ts, which is pure by design so it is testable without a
# robot on the desk -- same split as the K10's totem_logic.h.
check-bridge:
	cd bridge && npx tsc --noEmit
	cd bridge && npx vitest run

# The contract guard runs FIRST: if the office's payload has drifted, the
# bridge's types are wrong and its tests are asserting the wrong shape.
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

check: check-contract check-bridge check-pa

# NOT part of `check`: it clones two repositories. Run it after touching
# anything in firmware/*.sh. It catches a failure mode that is otherwise
# silent -- a second pass over an already-patched tree producing DIFFERENT
# sources from the first, so incremental builds ship different firmware from
# fresh ones. That happened once and nothing errored; the build just succeeded
# with `surprised` wearing narrower eyes.
check-idempotent:
	bash firmware/check-idempotent.sh
