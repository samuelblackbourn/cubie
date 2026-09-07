PYTHON ?= python3
VO_REPO ?= /workspace/virtual-office

.PHONY: help check check-contract check-bridge check-idempotent bridge-install

help:
	@echo "check           the green bar (no CI by design, same as the sibling repos)"
	@echo "check-contract  fail if /api/companion/status has drifted upstream"
	@echo "check-bridge    typecheck + test the bridge"
	@echo "check-idempotent  prove the firmware patch chain converges (clones, ~minutes)"
	@echo "bridge-install  npm install in bridge/"
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
check: check-contract check-bridge

# NOT part of `check`: it clones two repositories. Run it after touching
# anything in firmware/*.sh. It catches a failure mode that is otherwise
# silent -- a second pass over an already-patched tree producing DIFFERENT
# sources from the first, so incremental builds ship different firmware from
# fresh ones. That happened once and nothing errored; the build just succeeded
# with `surprised` wearing narrower eyes.
check-idempotent:
	bash firmware/check-idempotent.sh
