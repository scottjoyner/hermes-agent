#!/bin/bash
# NAS Health Check Script
# Checks NAS connectivity, mount status, disk usage, and service health

set -e

NAS_PATH="/media/scott/NAS/fileserver"
LOG_FILE="/var/log/nas-health-check.log"
ALERT_EMAIL="scott@example.com"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

check_nas_mount() {
    log "Checking NAS mount status..."
    if mountpoint -q "$NAS_PATH"; then
        log "${GREEN}NAS is mounted at $NAS_PATH${NC}"
        return 0
    else
        log "${RED}NAS is NOT mounted at $NAS_PATH${NC}"
        return 1
    fi
}

check_disk_usage() {
    log "Checking disk usage..."
    local usage=$(df "$NAS_PATH" | tail -1 | awk '{print $5}' | sed 's/%//')
    if [ "$usage" -gt 90 ]; then
        log "${RED}CRITICAL: NAS disk usage is ${usage}%${NC}"
        return 1
    elif [ "$usage" -gt 80 ]; then
        log "${YELLOW}WARNING: NAS disk usage is ${usage}%${NC}"
        return 0
    else
        log "${GREEN}NAS disk usage is ${usage}%${NC}"
        return 0
    fi
}

check_services() {
    log "Checking Docker services..."
    local services=("neo4j" "nextcloud" "signal-cli" "nginx")
    local failed=0
    
    for service in "${services[@]}"; do
        if docker ps --format '{{.Names}}' | grep -q "^${service}$"; then
            log "${GREEN}$service is running${NC}"
        else
            log "${RED}$service is NOT running${NC}"
            failed=$((failed + 1))
        fi
    done
    
    return $failed
}

check_nas_writable() {
    log "Checking NAS writability..."
    local test_file="$NAS_PATH/.health_check_test"
    if touch "$test_file" 2>/dev/null; then
        rm -f "$test_file"
        log "${GREEN}NAS is writable${NC}"
        return 0
    else
        log "${RED}NAS is NOT writable${NC}"
        return 1
    fi
}

# Main execution
log "=== Starting NAS Health Check ==="

check_nas_mount || true
check_disk_usage || true
check_services || true
check_nas_writable || true

log "=== NAS Health Check Complete ==="
