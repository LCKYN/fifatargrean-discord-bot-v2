#!/bin/bash

# ===========================================
# Server Security & Setup Script
# For Ubuntu DigitalOcean Droplet
# ===========================================

set -e

echo "=========================================="
echo "  Server Security & Setup Script"
echo "=========================================="

# Update system
echo "[1/8] Updating system packages..."
apt update && apt upgrade -y

# Install essential packages
echo "[2/8] Installing essential packages..."
apt install -y \
    ufw \
    fail2ban \
    unattended-upgrades \
    apt-listchanges \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    git

# ===========================================
# Configure UFW Firewall
# ===========================================
echo "[3/8] Configuring UFW firewall..."

# Reset UFW to default
ufw --force reset

# Default policies - deny all incoming, allow all outgoing
ufw default deny incoming
ufw default allow outgoing

# Allow SSH (important - don't lock yourself out!)
ufw allow ssh
ufw allow 22/tcp

# Allow HTTP and HTTPS (if you need web access later)
# ufw allow 80/tcp
# ufw allow 443/tcp

# IMPORTANT: Do NOT allow 5432 (PostgreSQL) - keep it internal only!
# Do NOT allow 3306 (MySQL)
# Do NOT allow 6379 (Redis)

# Enable UFW
ufw --force enable

echo "UFW Status:"
ufw status verbose

# ===========================================
# Configure Fail2Ban (Brute Force Protection)
# ===========================================
echo "[4/8] Configuring Fail2Ban..."

cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
# Ban for 1 hour
bantime = 3600
# Check last 10 minutes
findtime = 600
# Ban after 5 failed attempts
maxretry = 5
# Use UFW for banning
banaction = ufw

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 86400

# Protect against port scanning
[portscan]
enabled = true
filter = portscan
logpath = /var/log/syslog
maxretry = 3
bantime = 86400
EOF

# Create portscan filter
cat > /etc/fail2ban/filter.d/portscan.conf << 'EOF'
[Definition]
failregex = UFW BLOCK.* SRC=<HOST>
ignoreregex =
EOF

# Restart fail2ban
systemctl enable fail2ban
systemctl restart fail2ban

echo "Fail2Ban Status:"
fail2ban-client status

# ===========================================
# Configure Automatic Security Updates
# ===========================================
echo "[5/8] Configuring automatic security updates..."

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

# ===========================================
# SSH Hardening
# ===========================================
echo "[6/8] Hardening SSH configuration..."

# Backup original config
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup

# Apply hardening (be careful - make sure you have SSH key access!)
cat >> /etc/ssh/sshd_config << 'EOF'

# Security Hardening
Protocol 2
PermitRootLogin prohibit-password
MaxAuthTries 3
LoginGraceTime 30
PasswordAuthentication no
PermitEmptyPasswords no
X11Forwarding no
AllowTcpForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
EOF

# Note: Only restart SSH if you're sure you have key-based access
# systemctl restart sshd

echo "WARNING: SSH hardening applied but NOT restarted."
echo "Make sure you have SSH key access before running: systemctl restart sshd"

# ===========================================
# Install Docker
# ===========================================
echo "[7/8] Installing Docker..."

# Add Docker's official GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Start and enable Docker
systemctl enable docker
systemctl start docker

echo "Docker version:"
docker --version
docker compose version

# ===========================================
# Create app directory
# ===========================================
echo "[8/8] Setting up application directory..."

mkdir -p /root/fifatargrean-discord-bot-v2
cd /root/fifatargrean-discord-bot-v2

echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Clone your repo:"
echo "   cd /root && git clone https://github.com/LCKYN/fifatargrean-discord-bot-v2.git"
echo ""
echo "2. Create .env.secret file with your secrets:"
echo "   nano /root/fifatargrean-discord-bot-v2/.env.secret"
echo ""
echo "3. Start the bot:"
echo "   cd /root/fifatargrean-discord-bot-v2 && docker compose up -d"
echo ""
echo "4. Check logs:"
echo "   docker compose logs -f"
echo ""
echo "=========================================="
echo "  Security Summary"
echo "=========================================="
echo "✅ UFW Firewall enabled (only SSH allowed)"
echo "✅ Fail2Ban configured (brute force protection)"
echo "✅ Automatic security updates enabled"
echo "✅ SSH hardening applied"
echo "✅ Docker installed"
echo "✅ PostgreSQL port 5432 NOT exposed"
echo ""
echo "⚠️  IMPORTANT: If using password SSH, add your key first:"
echo "    ssh-copy-id root@YOUR_SERVER_IP"
echo "    Then: systemctl restart sshd"
echo ""
