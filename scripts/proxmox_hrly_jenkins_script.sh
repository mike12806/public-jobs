#!/bin/bash

# Function to log messages with timestamp
log_message() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') - $1"
}

# Function to check if the script is run as root
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_message "This script must be run as root or with sudo."
        exit 1
    fi
}

# Function to update the system packages
update_system() {
    log_message "Updating system packages..."
    apt-get update -y && \
    apt-get dist-upgrade -y && \
    apt-get autoremove -y && \
    apt-get clean -y && \
    apt-get autoclean -y
}

# Function to set the timezone to Eastern Time (America/New_York)
set_timezone() {
    log_message "Setting timezone to Eastern Time (America/New_York)..."
    timedatectl set-timezone America/New_York
}

# Function to set the NTP server to 192.168.1.103:123, 192.168.9.7:123, and 192.168.9.3:123
set_ntp_server() {
    log_message "Setting custom NTP server (192.168.1.103:123, 192.168.9.7:123, and 192.168.9.3:123)..."

    # Edit the systemd-timesyncd config file to set custom NTP server
    echo "NTP=192.168.1.103 192.168.9.7 192.168.9.3" >> /etc/systemd/timesyncd.conf

    # Restart systemd-timesyncd service to apply the changes
    systemctl restart systemd-timesyncd

    # Enable the NTP service to start on boot
    systemctl enable systemd-timesyncd

    # Force immediate time synchronization
    timedatectl set-ntp true

    # Wait a moment and force another sync
    sleep 5
    systemctl restart systemd-timesyncd

    # Display current time sync status
    log_message "Current time sync status:"
    timedatectl timesync-status || timedatectl status

    log_message "NTP server set to 192.168.1.103:123, 192.168.9.7:123, and 192.168.9.3:123 and time synchronized."
}

# Get and echo local IP addresses
log_message "Local IP addresses:"
hostname -I

# Main script execution
main() {
    # Check if running as root
    check_root

    # Update system packages
    update_system

    # Set timezone to Eastern Time
    set_timezone

    # Set the NTP server
    set_ntp_server

    # Read interfaces from /etc/network/interfaces and filter those starting with 'e'
    interfaces=$(grep -oP '^\s*auto\s+\K(e\w+)' /etc/network/interfaces | sort -u)

    if [ -z "$interfaces" ]; then
        log_message "No interfaces starting with 'e' found."
        exit 0
    fi

    # Bring up interfaces starting with 'e'
    for interface in $interfaces; do
        # Check if the interface is down
        if ! ip link show "$interface" | grep -q 'state UP'; then
            log_message "Bringing up interface: $interface"
            ifup "$interface"
            if [ $? -eq 0 ]; then
                log_message "Successfully brought up interface: $interface"
            else
                log_message "Failed to bring up interface: $interface"
            fi
        else
            log_message "Interface $interface is already up."
        fi
    done

    log_message "System maintenance complete!"
}

# Run the main function
main
