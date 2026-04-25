#!/bin/bash
# Setup Script - Twitter Bot Pipeline V2
# Automated installation and configuration

set -euo pipefail

echo "🤖 Twitter Bot Pipeline V2 - Setup"
echo "======================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    log_error "Please do not run as root. Run as regular user with sudo access."
    exit 1
fi

# Check OS
if [ ! -f /etc/os-release ]; then
    log_error "Cannot detect OS. Ubuntu 22.04+ required."
    exit 1
fi

source /etc/os-release
if [[ "$ID" != "ubuntu" ]]; then
    log_warn "This script is designed for Ubuntu. You're running $ID."
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

log_info "OS: $PRETTY_NAME"

# Update system
echo ""
echo "📦 Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# Install Python 3.11+
echo ""
echo "🐍 Installing Python 3.11..."
if ! command -v python3.11 &> /dev/null; then
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
    log_info "Python 3.11 installed"
else
    log_info "Python 3.11 already installed"
fi

# Install Node.js 18+
echo ""
echo "📦 Installing Node.js 18..."
if ! command -v node &> /dev/null || [ "$(node -v | cut -d'v' -f2 | cut -d'.' -f1)" -lt 18 ]; then
    # Install Node.js via signed apt repository (no curl|bash)
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | sudo tee /etc/apt/sources.list.d/nodesource.list
    sudo apt-get update -qq
    sudo apt-get install -y nodejs
    log_info "Node.js 22 installed"
else
    log_info "Node.js 18+ already installed"
fi

# Install PM2
echo ""
echo "🔄 Installing PM2..."
if ! command -v pm2 &> /dev/null; then
    sudo npm install -g pm2
    pm2 startup systemd -u $USER --hp $HOME
    log_info "PM2 installed"
else
    log_info "PM2 already installed"
fi

# Install Docker
echo ""
echo "🐳 Installing Docker..."
if ! command -v docker &> /dev/null; then
    # Install Docker via signed apt repository (no curl|bash)
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
    sudo apt-get update -qq
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io
    sudo usermod -aG docker "$USER"
    log_warn "Added $USER to docker group. You may need to log out and back in."
    log_info "Docker installed"
else
    log_info "Docker already installed"
fi

# Install PostgreSQL client
echo ""
echo "🐘 Installing PostgreSQL client..."
if ! command -v psql &> /dev/null; then
    sudo apt-get install -y postgresql-client
    log_info "PostgreSQL client installed"
else
    log_info "PostgreSQL client already installed"
fi

# Install security tools
echo ""
echo "🔒 Installing security tools..."
pip3 install bandit semgrep --quiet
log_info "Security tools installed (Bandit, Semgrep)"

# Install Mozilla sops
echo ""
echo "🔐 Installing Mozilla sops..."
if ! command -v sops &> /dev/null; then
    SOPS_VERSION="3.8.1"
    wget -q https://github.com/mozilla/sops/releases/download/v${SOPS_VERSION}/sops_${SOPS_VERSION}_amd64.deb
    sudo dpkg -i sops_${SOPS_VERSION}_amd64.deb
    rm sops_${SOPS_VERSION}_amd64.deb
    log_info "Mozilla sops installed"
else
    log_info "Mozilla sops already installed"
fi

# Install Python dependencies
echo ""
echo "📚 Installing Python dependencies..."
cd /home/ubuntu/openclaw
if [ -f requirements.txt ]; then
    pip3 install -r requirements.txt --quiet
    log_info "Python dependencies installed"
else
    log_warn "requirements.txt not found, skipping"
fi

# Create directory structure
echo ""
echo "📁 Creating directory structure..."
mkdir -p models data logs backup config
chmod +x scripts/utils/scrapling_medic.sh
log_info "Directories created"

# Check for .env configuration
echo ""
echo "🔧 Configuration check..."
if [ -f /dev/shm/.env ]; then
    log_info ".env found in /dev/shm (secure tmpfs)"
elif [ -f config/.env.enc ]; then
    log_warn ".env.enc found but not decrypted. Run: sops --decrypt config/.env.enc > /dev/shm/.env"
else
    log_error ".env configuration missing!"
    echo ""
    echo "Create config/.env.enc with these variables:"
    echo "  DATABASE_URL="
    echo "  OPENROUTER_API_KEY="
    echo "  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET, X_BEARER_TOKEN"
    echo "  TELEGRAM_BOT_TOKEN="
    echo "  TELEGRAM_ALLOWED_USER_IDS="
    echo ""
    echo "Then encrypt with: sops --encrypt config/.env > config/.env.enc"
fi

# Database setup
echo ""
echo "🗄️  Database setup..."
if [ -f /dev/shm/.env ]; then
    # Validate .env contains only safe KEY=VALUE lines before sourcing
    if grep -qE '[;`]|\\$\\(' /dev/shm/.env; then
        log_error ".env contains suspicious characters. Review manually before sourcing."
        exit 1
    fi
    set -a
    source /dev/shm/.env
    set +a
    
    if [ -n "$DATABASE_URL" ]; then
        echo "Testing database connection..."
        if psql "$DATABASE_URL" -c "SELECT 1" > /dev/null 2>&1; then
            log_info "Database connection successful"
            
            read -p "Execute schema.sql? (y/N): " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                psql "$DATABASE_URL" < schema.sql
                log_info "Schema executed"
            fi
        else
            log_error "Database connection failed. Check DATABASE_URL"
        fi
    else
        log_warn "DATABASE_URL not set in .env"
    fi
else
    log_warn "Skipping database setup (.env not found)"
fi

# PM2 setup
echo ""
echo "🚀 PM2 setup..."
if [ -f ecosystem.config.js ]; then
    log_info "ecosystem.config.js found"
    
    read -p "Start PM2 services now? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        pm2 delete all 2>/dev/null || true
        pm2 start ecosystem.config.js
        pm2 save
        log_info "PM2 services started"
        echo ""
        pm2 status
    fi
else
    log_warn "ecosystem.config.js not found"
fi

# Summary
echo ""
echo "======================================"
echo "✅ Setup Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "  1. Configure secrets: sops --decrypt config/.env.enc > /dev/shm/.env"
echo "  2. Execute schema: psql \$DATABASE_URL < schema.sql"
echo "  3. Start services: pm2 start ecosystem.config.js"
echo "  4. Monitor logs: pm2 logs"
echo ""
echo "Documentation: README_DEPLOYMENT.md"
echo "Architecture: TWITTER_BOT_PIPELINE.md"
echo ""
echo "🎉 Ready to deploy!"
