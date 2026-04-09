#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run expense-service integration tests (Python + Cypress) inside containers.
#
# Stages:
#   1. Start ERPNext container (dev-central-site)
#   2. Wait for Frappe site to be ready
#   3. Bootstrap ERPNext test data if absent
#   4. Install frappe-microservice lib and test dependencies
#   5. Run Python integration tests (pytest + coverage)
#   6. Start the expense-service HTTP server on port 9004
#   7. Run Cypress API tests via the cypress container (same network)
#
# Usage:
#   ./scripts/run_integration_tests.sh            # full cycle: up → test → down
#   KEEP_RUNNING=1 ./scripts/run_integration_tests.sh   # leave containers up
#   SKIP_PYTHON=1  ./scripts/run_integration_tests.sh   # skip Python tests
#   SKIP_CYPRESS=1 ./scripts/run_integration_tests.sh   # skip Cypress tests
# ---------------------------------------------------------------------------
set -euo pipefail

COMPOSE_FILE="docker-compose.integration.yml"
SERVICE="dev-central-site"
LIB_MOUNT="/mnt/lib"
EXPENSE_MOUNT="/mnt/expense"
EXPENSE_PORT=9004
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
        log "KEEP_RUNNING=1 — containers left running"
    else
        log "Tearing down"
        $COMPOSE -f "$COMPOSE_FILE" --profile cypress down -v 2>/dev/null || true
    fi
}
trap cleanup EXIT

BENCH="/home/frappe/frappe-bench"
PYTHON="$BENCH/env/bin/python"

# ---------------------------------------------------------------------------
# 1. Start ERPNext container
# ---------------------------------------------------------------------------
log "Starting ERPNext container"
$COMPOSE -f "$COMPOSE_FILE" up -d

# ---------------------------------------------------------------------------
# 2. Wait for Frappe site
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 3. Bootstrap ERPNext test data if needed
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 4. Install frappe-microservice lib and test dependencies
# ---------------------------------------------------------------------------
log "Installing frappe-microservice and test dependencies"
$COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
    "$BENCH/env/bin/pip" install --quiet "$LIB_MOUNT" pytest pytest-cov

# ---------------------------------------------------------------------------
# 5. Python integration tests
# ---------------------------------------------------------------------------
if [[ "${SKIP_PYTHON:-}" != "1" ]]; then
    log "Running Python integration tests"
    $COMPOSE -f "$COMPOSE_FILE" exec -T -e "PYTHONPATH=$EXPENSE_MOUNT" "$SERVICE" \
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
    log "Python integration tests passed"
fi

# ---------------------------------------------------------------------------
# 6a. Seed Administrator's tenant_id so secure_route can resolve the tenant
#     for Cypress API tests (get_user_tenant_id reads tabUser.tenant_id).
# ---------------------------------------------------------------------------
log "Seeding Administrator tenant_id and company default for Cypress HTTP tests"
$COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" "$PYTHON" - << 'PYEOF'
import frappe
frappe.init(site='dev.localhost', sites_path='/home/frappe/frappe-bench/sites')
frappe.connect()
TEST_TENANT_ID = "expense-integ-tenant-001"
TEST_COMPANY   = "_Test Expense Integ Co"

# tenant_id already exists on tabUser in the central-site image.
frappe.db.sql(
    "UPDATE `tabUser` SET tenant_id = %s WHERE name = 'Administrator'",
    (TEST_TENANT_ID,),
)

# Ensure Administrator has a default company so _resolve_company() works in HTTP context
existing = frappe.db.get_value(
    "DefaultValue", {"parent": "Administrator", "defkey": "company"}, "name"
)
if existing:
    frappe.db.set_value("DefaultValue", existing, "defvalue", TEST_COMPANY)
else:
    frappe.get_doc({
        "doctype": "DefaultValue",
        "parent": "Administrator",
        "parenttype": "User",
        "parentfield": "defaults",
        "defkey": "company",
        "defvalue": TEST_COMPANY,
    }).insert(ignore_permissions=True)

frappe.db.commit()
print(f"  Administrator.tenant_id = {TEST_TENANT_ID}")
print(f"  Administrator default company = {TEST_COMPANY}")
frappe.destroy()
PYEOF

# ---------------------------------------------------------------------------
# 6. Start expense-service HTTP server on port 9004 (inside the container)
# ---------------------------------------------------------------------------
log "Starting expense-service on port $EXPENSE_PORT"
# Discover the active site name from currentsite.txt — same approach used by bench migrate
FRAPPE_SITE=$($COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
    bash -c "cat $BENCH/sites/currentsite.txt 2>/dev/null || echo dev.localhost" | tr -d '\r\n')
log "  using Frappe site: $FRAPPE_SITE"

$COMPOSE -f "$COMPOSE_FILE" exec -d "$SERVICE" bash -c "
    export FRAPPE_SITES_PATH=$BENCH/sites
    export FRAPPE_SITE=$FRAPPE_SITE
    export SERVICE_PORT=$EXPENSE_PORT
    # --chdir keeps CWD inside the bench so Frappe writes logs to the bench
    # log dir (/home/frappe/frappe-bench/logs/), not to a stale CWD.
    # --pythonpath adds the expense-service mount to sys.path.
    $BENCH/env/bin/gunicorn \
        --chdir $BENCH \
        --pythonpath $EXPENSE_MOUNT \
        --bind 0.0.0.0:$EXPENSE_PORT \
        --workers 2 \
        --timeout 120 \
        server:app \
        >> /home/frappe/logs/expense-service.log 2>&1
"

# Wait for the expense-service to be ready
log "Waiting for expense-service on port $EXPENSE_PORT"
for i in $(seq 1 20); do
    if $COMPOSE -f "$COMPOSE_FILE" exec -T "$SERVICE" \
        curl -sf "http://localhost:$EXPENSE_PORT/health" &>/dev/null; then
        echo "  expense-service is up"; break
    fi
    printf "  attempt %d/20 ...\n" "$i"
    sleep 3
done

# ---------------------------------------------------------------------------
# 7. Cypress API tests
# ---------------------------------------------------------------------------
if [[ "${SKIP_CYPRESS:-}" != "1" ]]; then
    log "Running Cypress API tests"
    $COMPOSE -f "$COMPOSE_FILE" --profile cypress run --rm cypress
    log "Cypress API tests passed"
fi

log "All tests passed"
