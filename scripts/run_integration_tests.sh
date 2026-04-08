#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run expense-service integration tests inside the ERPNext container.
#
# Usage:
#   ./scripts/run_integration_tests.sh            # full cycle: up → test → down
#   KEEP_RUNNING=1 ./scripts/run_integration_tests.sh   # leave container up
# ---------------------------------------------------------------------------
set -euo pipefail

COMPOSE_FILE="docker-compose.integration.yml"
SERVICE="dev-central-site"
LIB_MOUNT="/mnt/lib"
EXPENSE_MOUNT="/mnt/expense"
MAX_WAIT=40
WAIT_INTERVAL=10

if command -v docker &>/dev/null; then
    COMPOSE="docker compose"
elif command -v podman &>/dev/null; then
    COMPOSE="podman compose"
else
    echo "FATAL: Neither docker nor podman found in PATH" >&2; exit 1
fi

log()  { printf "\n=== %s ===\n" "$*"; }
fail() { echo "FATAL: $*" >&2; exit 1; }

cleanup() {
    if [[ "${KEEP_RUNNING:-}" == "1" ]]; then
        log "KEEP_RUNNING=1 — container left running"
    else
        log "Tearing down"
        $COMPOSE -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    fi
}
trap cleanup EXIT

BENCH="/home/frappe/frappe-bench"
PYTHON="$BENCH/env/bin/python"

# -- start container --------------------------------------------------------
log "Starting container"
$COMPOSE -f "$COMPOSE_FILE" up -d

# -- wait for Frappe site ---------------------------------------------------
log "Waiting for Frappe site (up to $((MAX_WAIT * WAIT_INTERVAL))s)"
READY=0
for i in $(seq 1 "$MAX_WAIT"); do
    if $COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
        bash -c "mkdir -p $BENCH/dev.localhost/logs /home/frappe/logs && \
        $PYTHON -c \"
import frappe
frappe.init(site='dev.localhost', sites_path='$BENCH/sites')
frappe.connect()
frappe.db.sql('SELECT 1')
frappe.destroy()
print('ready')
\"" 2>/dev/null; then
        READY=1; break
    fi
    printf "  attempt %d/%d ...\n" "$i" "$MAX_WAIT"
    sleep "$WAIT_INTERVAL"
done
[[ "$READY" == "1" ]] || fail "Site not ready after $((MAX_WAIT * WAIT_INTERVAL))s"

# -- bootstrap ERPNext test data if needed ----------------------------------
HAS_COMPANY=$($COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
    "$PYTHON" -c "
import frappe
frappe.init(site='dev.localhost', sites_path='$BENCH/sites')
frappe.connect()
print(frappe.db.count('Company'))
frappe.destroy()
" 2>/dev/null || echo "0")

if [[ "$HAS_COMPANY" == "0" ]]; then
    log "No Company found — running ERPNext before_tests bootstrap"
    $COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
        bash -c "cd $BENCH && bench --site dev.localhost execute erpnext.setup.utils.before_tests"
fi

# -- install frappe-microservice lib and test deps -------------------------
log "Installing frappe-microservice and test dependencies"
$COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
    "$BENCH/env/bin/pip" install --quiet "$LIB_MOUNT" pytest pytest-cov

# -- run integration tests -------------------------------------------------
log "Running expense-service integration tests"
$COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
    "$PYTHON" -m pytest \
        "$EXPENSE_MOUNT/tests/integration/" \
        --confcutdir="$EXPENSE_MOUNT/tests/integration" \
        -c "$EXPENSE_MOUNT/tests/integration/pytest.ini" \
        --cov=controllers \
        --cov=expense_tracker \
        --cov-report=term-missing \
        --cov-branch \
        -v --tb=short \
        --rootdir="$EXPENSE_MOUNT"

log "All integration tests passed"
