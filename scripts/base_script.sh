# Tailor to your specs

# Remove any existing script to avoid conflicts
rm -f /tmp/jenkins_hourly_script.sh

# Download the shell script from GitHub
wget https://git.mfaherty.net/mike12806/script.sh -O /tmp/jenkins_hourly_script.sh

# Make the script executable
chmod +x /tmp/jenkins_hourly_script.sh

# Run the script
/tmp/jenkins_hourly_script.sh
