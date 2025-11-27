#!/bin/bash

# Yank - LAN Clipboard Sync (macOS)
# This script provides macOS-specific features like LaunchAgent

# Configuration
SESSION_NAME="clipboard-sync"
VENV_PATH="venv"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/clipboard-sync.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.yank.clipboard-sync.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"

cd "$SCRIPT_DIR"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Function to display help
show_help() {
    cat << EOF
Yank - LAN Clipboard Sync (macOS)

Usage: ./macrun.sh [COMMAND] [OPTIONS]

Process Commands:
  start [OPTIONS]   Start clipboard sync (default)
    --peer IP       Connect to specific IP
    --verbose       Enable verbose logging
    --no-security   Disable encryption (not recommended)
  stop              Stop the running session
  restart           Restart the session
  attach            Attach to running tmux session
  logs              View the log file
  tail              Follow log file in real-time

Security Commands:
  pair              Enter pairing mode (display PIN)
  join <IP> <PIN>   Pair with another device
  unpair            Remove current pairing
  status            Show pairing and session status

Configuration:
  config                     Show current configuration
  config --set KEY VALUE     Set a configuration value
  config --reset             Reset to defaults

Auto-Start (macOS):
  install           Install LaunchAgent for auto-start on login
  uninstall         Remove LaunchAgent (disable auto-start)

Other:
  help              Show this help message

Examples:
  ./macrun.sh pair                          # Display PIN for pairing
  ./macrun.sh join 192.168.1.5 123456       # Pair with device
  ./macrun.sh start                         # Start syncing (encrypted)
  ./macrun.sh start --verbose               # Start with debug logging
  ./macrun.sh start --peer 192.168.1.5      # Connect to specific IP
  ./macrun.sh install                       # Enable auto-start on login
  ./macrun.sh config --set sync_text false  # Disable text sync

Files:
  Config:  sync_config.json
  Ignore:  .syncignore
  Logs:    $LOG_FILE

EOF
}

# Function to start the session
# Usage: start_session [--peer IP] [--verbose] [--no-security]
start_session() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' already running. Use 'attach' to connect or 'restart' to restart."
        return 1
    fi

    # Build extra arguments
    local extra_args="$*"

    echo "Creating new tmux session '$SESSION_NAME'..."

    # Create new tmux session in detached mode
    tmux new-session -d -s "$SESSION_NAME" -c "$SCRIPT_DIR"

    # Activate venv and run the Python module with logging
    tmux send-keys -t "$SESSION_NAME" "source $VENV_PATH/bin/activate && python -m main start $extra_args 2>&1 | tee -a $LOG_FILE" Enter

    echo "Session started in detached mode. Use './macrun.sh attach' to connect."
}

# Function to attach to the session
attach_session() {
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is not running. Use './macrun.sh start' to start it."
        return 1
    fi
    tmux attach-session -t "$SESSION_NAME"
}

# Function to view logs
view_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "Log file not found: $LOG_FILE"
        return 1
    fi
    cat "$LOG_FILE"
}

# Function to tail logs
tail_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "Log file not found: $LOG_FILE"
        return 1
    fi
    tail -f "$LOG_FILE"
}

# Function to restart the session
# Usage: restart_session [--peer IP] [--verbose] [--no-security]
restart_session() {
    echo "Restarting session '$SESSION_NAME'..."
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME"
        echo "Old session killed."
    fi
    sleep 1
    start_session "$@"
}

# Function to stop the session
stop_session() {
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is not running."
        return 1
    fi
    tmux kill-session -t "$SESSION_NAME"
    echo "Session '$SESSION_NAME' stopped."
}

# Function to show status
show_status() {
    echo "=== Session Status ==="
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "✓ Session '$SESSION_NAME' is running"
        echo ""
        echo "Session details:"
        tmux list-sessions -F "  #{session_name}: #{session_windows} window(s), #{session_attached} attached"
    else
        echo "✗ Session '$SESSION_NAME' is not running"
    fi

    echo ""
    echo "=== Auto-Start Status ==="
    if [ -f "$PLIST_PATH" ]; then
        if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
            echo "✓ LaunchAgent is installed and loaded (will start on boot)"
        else
            echo "⚠ LaunchAgent is installed but not loaded"
        fi
    else
        echo "✗ LaunchAgent is not installed (use './macrun.sh install' to enable auto-start)"
    fi
}

# Function to install LaunchAgent
install_launchagent() {
    echo "Installing LaunchAgent for auto-start on boot..."

    # Create LaunchAgents directory if it doesn't exist
    mkdir -p "$HOME/Library/LaunchAgents"

    # Create the plist file
    cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/macrun.sh</string>
        <string>start</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/launchd-error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF

    # Unload if already loaded
    launchctl unload "$PLIST_PATH" 2>/dev/null

    # Load the LaunchAgent
    if launchctl load "$PLIST_PATH"; then
        echo "✓ LaunchAgent installed and loaded successfully"
        echo "  clipboard-sync will now start automatically on login"
    else
        echo "✗ Failed to load LaunchAgent"
        return 1
    fi
}

# Function to uninstall LaunchAgent
uninstall_launchagent() {
    echo "Removing LaunchAgent..."

    if [ ! -f "$PLIST_PATH" ]; then
        echo "LaunchAgent is not installed."
        return 1
    fi

    # Unload the LaunchAgent
    launchctl unload "$PLIST_PATH" 2>/dev/null

    # Remove the plist file
    rm -f "$PLIST_PATH"

    echo "✓ LaunchAgent removed successfully"
    echo "  clipboard-sync will no longer start automatically on login"
}

# Function to start pairing mode
start_pairing() {
    echo "Starting pairing mode..."
    source "$VENV_PATH/bin/activate" && python -m main pair
}

# Function to join/pair with another device
join_device() {
    local host="$1"
    local pin="$2"

    if [ -z "$host" ]; then
        echo "Error: Host IP is required. Usage: ./macrun.sh join <IP> <PIN>"
        exit 1
    fi

    if [ -z "$pin" ]; then
        echo "Error: PIN is required. Usage: ./macrun.sh join <IP> <PIN>"
        exit 1
    fi

    echo "Connecting to $host..."
    source "$VENV_PATH/bin/activate" && python -m main join "$host" "$pin"
}

# Function to remove pairing
remove_pairing() {
    source "$VENV_PATH/bin/activate" && python -m main unpair
}

# Function to show security status
show_security() {
    source "$VENV_PATH/bin/activate" && python -m main status
}

# Function to show/edit configuration
show_config() {
    if [ "$1" = "--reset" ]; then
        source "$VENV_PATH/bin/activate" && python -m main config --reset
    elif [ "$1" = "--set" ] && [ -n "$2" ] && [ -n "$3" ]; then
        source "$VENV_PATH/bin/activate" && python -m main config --set "$2" "$3"
    else
        source "$VENV_PATH/bin/activate" && python -m main config
    fi
}

# Main command handling
COMMAND="${1:-start}"

case "$COMMAND" in
    start)
        shift  # Remove 'start' from args
        start_session "$@"  # Pass remaining args (--peer, --verbose, etc.)
        ;;
    attach)
        attach_session
        ;;
    logs)
        view_logs
        ;;
    tail)
        tail_logs
        ;;
    restart)
        shift  # Remove 'restart' from args
        restart_session "$@"  # Pass remaining args (--peer, --verbose, etc.)
        ;;
    stop)
        stop_session
        ;;
    status)
        show_status
        ;;
    pair)
        start_pairing
        ;;
    join)
        join_device "$2" "$3"
        ;;
    unpair)
        remove_pairing
        ;;
    security)
        show_security
        ;;
    config)
        show_config "$2" "$3" "$4"
        ;;
    install)
        install_launchagent
        ;;
    uninstall)
        uninstall_launchagent
        ;;
    help|--help|-h)
        show_help
        ;;
    --*)
        # Handle flags passed directly (./macrun.sh --verbose)
        start_session "$@"
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
