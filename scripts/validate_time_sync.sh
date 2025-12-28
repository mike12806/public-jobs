#!/bin/bash

# Time Sync Validation Script
# Checks if NTP configuration and time synchronization is working properly

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=== NTP Time Synchronization Validation ==="
echo ""

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${YELLOW}Warning: Not running as root. Some checks may fail.${NC}"
    echo ""
fi

# 1. Check timezone
echo "1. Checking timezone configuration..."
current_tz=$(timedatectl status | grep "Time zone" | awk '{print $3}')
if [ "$current_tz" = "America/New_York" ]; then
    echo -e "${GREEN}✓ Timezone is correctly set to America/New_York${NC}"
else
    echo -e "${RED}✗ Timezone is $current_tz (should be America/New_York)${NC}"
fi
echo ""

# 2. Check NTP configuration
echo "2. Checking NTP server configuration..."
if [ -f /etc/systemd/timesyncd.conf ]; then
    if grep -q "NTP=192.168.1.103 192.168.9.7 192.168.9.3" /etc/systemd/timesyncd.conf; then
        echo -e "${GREEN}✓ Custom NTP servers are configured${NC}"
    else
        echo -e "${RED}✗ Custom NTP servers not found in configuration${NC}"
    fi
else
    echo -e "${RED}✗ /etc/systemd/timesyncd.conf not found${NC}"
fi
echo ""

# 3. Check systemd-timesyncd service status
echo "3. Checking systemd-timesyncd service..."
if systemctl is-active --quiet systemd-timesyncd; then
    echo -e "${GREEN}✓ systemd-timesyncd service is active${NC}"
else
    echo -e "${RED}✗ systemd-timesyncd service is not active${NC}"
fi

if systemctl is-enabled --quiet systemd-timesyncd; then
    echo -e "${GREEN}✓ systemd-timesyncd service is enabled${NC}"
else
    echo -e "${RED}✗ systemd-timesyncd service is not enabled${NC}"
fi
echo ""

# 4. Check time synchronization status
echo "4. Checking time synchronization status..."
sync_status=$(timedatectl status | grep "synchronized" | awk '{print $4}')
if [ "$sync_status" = "yes" ]; then
    echo -e "${GREEN}✓ System clock is synchronized${NC}"
else
    echo -e "${RED}✗ System clock is not synchronized${NC}"
fi

ntp_status=$(timedatectl status | grep "NTP service" | awk '{print $3}')
if [ "$ntp_status" = "active" ]; then
    echo -e "${GREEN}✓ NTP service is active${NC}"
else
    echo -e "${RED}✗ NTP service is not active${NC}"
fi
echo ""

# 5. Show detailed time sync status
echo "5. Detailed time synchronization information..."
echo "--- timedatectl status ---"
timedatectl status
echo ""

# Try to show timesync-status if available
echo "--- timedatectl timesync-status ---"
if timedatectl timesync-status 2>/dev/null; then
    echo "Timesync status retrieved successfully"
else
    echo "Timesync-status not available (normal on some systems)"
fi
echo ""

# 6. Show current time
echo "6. Current system time..."
echo "Current time: $(date)"
echo "UTC time: $(date -u)"
echo ""

# 7. Summary
echo "=== SUMMARY ==="
error_count=0

if [ "$current_tz" != "America/New_York" ]; then
    ((error_count++))
fi

if ! grep -q "NTP=192.168.1.103 192.168.9.7 192.168.9.3" /etc/systemd/timesyncd.conf 2>/dev/null; then
    ((error_count++))
fi

if ! systemctl is-active --quiet systemd-timesyncd; then
    ((error_count++))
fi

if [ "$sync_status" != "yes" ]; then
    ((error_count++))
fi

if [ $error_count -eq 0 ]; then
    echo -e "${GREEN}✓ All time synchronization checks passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ $error_count time synchronization issues found${NC}"
    echo ""
    echo "To fix issues, run the following commands as root:"
    echo "1. timedatectl set-timezone America/New_York"
    echo "2. echo 'NTP=192.168.1.103 192.168.9.7 192.168.9.3' >> /etc/systemd/timesyncd.conf"
    echo "3. systemctl enable systemd-timesyncd"
    echo "4. systemctl restart systemd-timesyncd"
    echo "5. timedatectl set-ntp true"
    exit 1
fi
