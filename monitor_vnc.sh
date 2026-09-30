#!/bin/bash
# Monitor VNC proxy logs in real-time

echo "📊 VNC Console Connection Monitor"
echo "=================================="
echo "Watching for VNC proxy events..."
echo "Try accessing the console now at:"
echo "  https://cybersecurity.local/teacher/console/1"
echo ""
echo "Login: admin / admin"
echo ""
echo "Monitoring logs for 5 minutes..."
echo "=================================="
echo ""

journalctl -u cyberlab-admin -f --no-pager 2>&1 | grep -E "VNC-PROXY|Connected|Authentication|WebSocket|FATAL|Connected successfully|Error:" &
TAIL_PID=$!

# Wait 5 minutes
sleep 300

# Kill the tail
kill $TAIL_PID 2>/dev/null

echo ""
echo "=================================="
echo "Monitor stopped after 5 minutes"
