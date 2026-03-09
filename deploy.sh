#!/bin/bash
# Marco the Nomad — Conway sandbox deployment script
# Run this inside the Conway sandbox after creation
set -e

echo "=== Marco the Nomad — Deploying ==="

# Install system deps
apt-get update -qq && apt-get install -y -qq python3-pip python3-venv git > /dev/null 2>&1

# Clone repo
if [ ! -d /opt/marco ]; then
    git clone https://github.com/aarontekluuu/marco-nomad.git /opt/marco
else
    cd /opt/marco && git pull
fi

cd /opt/marco

# Create venv and install deps
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

# Check for .env
if [ ! -f .env ]; then
    echo ""
    echo "=== Configure .env ==="
    echo "Copy .env.template to .env and set:"
    echo "  ANTHROPIC_API_KEY=..."
    echo "  TELEGRAM_BOT_TOKEN=..."
    echo "  TELEGRAM_CHAT_ID=..."
    echo "  DEMO_MODE=false  (for live trading)"
    echo "  LOOP_INTERVAL=900  (15min cycles)"
    echo ""
    cp .env.template .env
    echo "Edit /opt/marco/.env then run: ./deploy.sh start"
    exit 0
fi

# Start or restart Marco
if [ "$1" = "start" ]; then
    echo "Starting Marco..."
    source venv/bin/activate
    nohup python main.py > /var/log/marco.log 2>&1 &
    echo $! > /tmp/marco.pid
    echo "Marco started (PID: $(cat /tmp/marco.pid))"
    echo "Logs: tail -f /var/log/marco.log"
elif [ "$1" = "stop" ]; then
    if [ -f /tmp/marco.pid ]; then
        kill $(cat /tmp/marco.pid) 2>/dev/null || true
        rm /tmp/marco.pid
        echo "Marco stopped"
    else
        echo "No PID file found"
    fi
elif [ "$1" = "logs" ]; then
    tail -f /var/log/marco.log
else
    echo "Usage: ./deploy.sh [start|stop|logs]"
    echo "  start — start Marco in background"
    echo "  stop  — stop Marco"
    echo "  logs  — tail the log file"
fi
