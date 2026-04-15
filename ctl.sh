#!/bin/bash
# Telegram Bridge Controller
# Usage: ./ctl.sh <start|stop|restart|status|logs> <agent_name|all>

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$BRIDGE_DIR/venv/bin/python3"

start_agent() {
    local agent="$1"
    local agent_dir="$BRIDGE_DIR/agents/$agent"
    local log_file="$agent_dir/bridge.log"
    local pid_file="$agent_dir/bridge.pid"
    
    if [ ! -f "$agent_dir/config.env" ]; then
        echo "❌ No config.env found for $agent"
        return 1
    fi
    
    # Check if already running
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "⚠️  $agent already running (PID $(cat "$pid_file"))"
        return 0
    fi
    
    echo "Starting $agent..."
    cd "$BRIDGE_DIR" && PYTHONUNBUFFERED=1 nohup "$PYTHON" -u run.py "agents/$agent" >> "$log_file" 2>&1 &
    echo $! > "$pid_file"
    echo "✅ $agent started (PID $!)"
}

stop_agent() {
    local agent="$1"
    local pid_file="$BRIDGE_DIR/agents/$agent/bridge.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "🛑 $agent stopped (PID $pid)"
        else
            echo "⚠️  $agent not running (stale PID $pid)"
        fi
        rm -f "$pid_file"
    else
        # Try pkill as fallback
        pkill -f "run.py agents/$agent" 2>/dev/null
        echo "🛑 $agent stopped (pkill)"
    fi
}

status_agent() {
    local agent="$1"
    local pid_file="$BRIDGE_DIR/agents/$agent/bridge.pid"
    
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "✅ $agent: running (PID $(cat "$pid_file"))"
    else
        echo "❌ $agent: stopped"
    fi
}

logs_agent() {
    local agent="$1"
    tail -20 "$BRIDGE_DIR/agents/$agent/bridge.log" 2>/dev/null || echo "No logs for $agent"
}

get_agents() {
    ls -d "$BRIDGE_DIR/agents"/*/config.env 2>/dev/null | while read f; do
        basename "$(dirname "$f")"
    done
}

ACTION="$1"
AGENT="$2"

if [ -z "$ACTION" ]; then
    echo "Usage: $0 <start|stop|restart|status|logs> <agent_name|all>"
    echo ""
    echo "Agents:"
    get_agents | while read a; do echo "  $a"; done
    exit 1
fi

if [ "$AGENT" = "all" ] || [ -z "$AGENT" ]; then
    agents=$(get_agents)
else
    agents="$AGENT"
fi

for agent in $agents; do
    case "$ACTION" in
        start)   start_agent "$agent" ;;
        stop)    stop_agent "$agent" ;;
        restart) stop_agent "$agent"; sleep 2; start_agent "$agent" ;;
        status)  status_agent "$agent" ;;
        logs)    echo "=== $agent ==="; logs_agent "$agent"; echo "" ;;
        *)       echo "Unknown action: $ACTION" ;;
    esac
done
