#!/bin/bash

# Function to check if the script is run as root
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "This script must be run as root or with sudo."
        exit 1
    fi
}

# Function to update the system packages while automatically keeping local configuration files
update_system() {
    echo "Updating system packages..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y && \
    apt-get full-upgrade -y --allow-downgrades --allow-remove-essential --allow-change-held-packages \
        -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" && \
    apt-get autoremove -y && \
    apt-get clean -y && \
    apt-get autoclean -y
}

# Function to set the timezone to America/New_York
set_timezone() {
    echo "Setting timezone to Eastern Time (America/New_York)..."
    timedatectl set-timezone America/New_York
}

# Function to set the NTP server
set_ntp_server() {
    echo "Setting custom NTP server (192.168.1.103:123, 192.168.9.7:123, and 192.168.9.3:123)..."

    # Edit the systemd-timesyncd config file to set custom NTP server
    echo "NTP=192.168.1.103 192.168.9.7 192.168.9.3" >> /etc/systemd/timesyncd.conf

    # Restart systemd-timesyncd service to apply the changes
    systemctl restart systemd-timesyncd

    # Enable the NTP service to start on boot
    systemctl enable systemd-timesyncd

    echo "NTP server set to 192.168.1.103:123, 192.168.9.7:123, and 192.168.9.3:123."
}

# Function to check NTP service status
check_ntp() {
    echo "Checking NTP service status..."
    systemctl status systemd-timesyncd | grep "Active" || echo "NTP service not active."
}

# Main script execution
main() {
    # Check if running as root
    check_root

    # Get and display local IP addresses
    echo "Local IP addresses:"
    hostname -I

    # Update system
    update_system

    # Set timezone
    set_timezone

    # Set the NTP server
    set_ntp_server

    # Verify timezone update
    echo "Timezone has been updated. Current system time:"
    timedatectl status

    # Check NTP status
    check_ntp

    # Completion message
    echo "Time zone update and system maintenance complete!"
}

# Run the main function
main
