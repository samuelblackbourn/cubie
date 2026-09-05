PYTHON ?= python3
VO_REPO ?= /workspace/virtual-office

.PHONY: help check check-contract

help:
	@echo "check           the green bar (no CI by design, same as the sibling repos)"
	@echo "check-contract  fail if /api/companion/status has drifted upstream"
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

check: check-contract
