#!/bin/bash
# Cut over from v1 (port 3031) to v2 (port 3031)
# Run as sudo on the cloud server.
set -e

V1_DIR=/root/coordinator-server
V2_DIR=/root/coordinator-server-v2

echo "===== STOP v1 ====="
systemctl stop coordinator.service
echo "v1 stopped"

echo "===== ARCHIVE v1 data into v2 (one-time import) ====="
# v2 server.js imports legacy shops.json on first boot if its DB is empty.
# We need to make the data available where v2 expects it.
mkdir -p $V2_DIR/data
if [ -f $V1_DIR/data/shops.json ]; then
    cp -n $V1_DIR/data/shops.json $V2_DIR/data/shops.json
    echo "  copied $V1_DIR/data/shops.json to $V2_DIR/data/shops.json"
fi

echo "===== UPDATE systemd unit to point at v2 ====="
cat > /etc/systemd/system/coordinator.service << 'EOF'
[Unit]
Description=Mercari Collector Coordinator v2
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/coordinator-server-v2
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=10
StandardOutput=append:/var/log/coordinator.log
StandardError=append:/var/log/coordinator.log
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "  systemd unit updated"

echo "===== START v2 ====="
systemctl start coordinator.service
sleep 3
systemctl status coordinator.service --no-pager | head -12

echo "===== VERIFY ====="
echo "GET /health:"
curl -sf http://localhost:3031/health
echo ""
echo "GET /api/summary (truncated):"
curl -sf http://localhost:3031/api/summary | head -c 400
echo ""
echo ""
echo "===== DONE ====="
echo "Dashboard: http://<RELAY_IP_REDACTED>:3031/"
