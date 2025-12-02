#!/bin/bash

# Yank - LAN Clipboard Sync
# Cross-platform script for Linux and macOS

# Detect OS
OS_TYPE="$(uname -s)"
IS_MACOS=false
IS_LINUX=false
if [ "$OS_TYPE" = "Darwin" ]; then
    IS_MACOS=true
elif [ "$OS_TYPE" = "Linux" ]; then
    IS_LINUX=true
fi

# Configuration
SESSION_NAME="clipboard-sync"
VENV_PATH="venv"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/clipboard-sync.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=9876

# macOS-specific config
if $IS_MACOS; then
    PLIST_NAME="com.yank.clipboard-sync.plist"
    PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
fi

# Linux-specific config
if $IS_LINUX; then
    SERVICE_NAME="yank-clipboard-sync.service"
    SERVICE_PATH="$HOME/.config/systemd/user/$SERVICE_NAME"
fi

cd "$SCRIPT_DIR"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Function to display help
show_help() {
    local script_name="./run.sh"

    cat << EOF
Yank - LAN Clipboard Sync

Usage: $script_name [COMMAND] [OPTIONS]

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
EOF

    if $IS_MACOS; then
        cat << EOF

Auto-Start (macOS):
  install           Install LaunchAgent for auto-start on login
  uninstall         Remove LaunchAgent (disable auto-start)
EOF
    elif $IS_LINUX; then
        cat << EOF

Auto-Start (Linux):
  install           Install systemd user service for auto-start
  uninstall         Remove systemd user service (disable auto-start)
EOF
    fi

    cat << EOF

Other:
  help              Show this help message

Examples:
  $script_name pair                          # Display PIN for pairing
  $script_name join 192.168.1.5 123456       # Pair with device
  $script_name start                         # Start syncing (encrypted)
  $script_name start --verbose               # Start with debug logging
  $script_name start --peer 192.168.1.5      # Connect to specific IP
  $script_name config --set sync_text false  # Disable text sync
EOF

    if $IS_MACOS || $IS_LINUX; then
        echo "  $script_name install                       # Enable auto-start on login"
    fi

    cat << EOF

Files:
  Config:  config.json
  Ignore:  .syncignore
  Logs:    $LOG_FILE

EOF
}

# Function to check if port 9876 is in use
is_port_in_use() {
    if $IS_MACOS; then
        lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1
    else
        netstat -tuln 2>/dev/null | grep -q ":$PORT " || ss -tuln 2>/dev/null | grep -q ":$PORT "
    fi
}

# Function to get PID using port 9876
get_pid_by_port() {
    if $IS_MACOS; then
        lsof -i :$PORT -sTCP:LISTEN -t 2>/dev/null | head -1
    else
        # Try netstat first, then ss
        local pid=$(netstat -tulnp 2>/dev/null | grep ":$PORT " | awk '{print $7}' | cut -d'/' -f1 | head -1)
        if [ -z "$pid" ]; then
            pid=$(ss -tulnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K\d+' | head -1)
        fi
        echo "$pid"
    fi
}

# Function to check if session is running
is_running() {
    # First check port (most reliable)
    if is_port_in_use; then
        return 0
    fi

    # Fallback to tmux session
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        return 0
    fi

    return 1
}

# Function to start the session
# Usage: start_session [--peer IP] [--verbose] [--no-security]
start_session() {
    if is_running; then
        echo "Session '$SESSION_NAME' already running. Use 'attach' to connect or 'restart' to restart."
        return 1
    fi

    # Build extra arguments
    local extra_args="$*"

    echo "Creating new tmux session '$SESSION_NAME'..."

    # Create new tmux session in detached mode
    tmux new-session -d -s "$SESSION_NAME" -c "$SCRIPT_DIR"

    # Activate venv and run the Python module with logging
    tmux send-keys -t "$SESSION_NAME" "source $VENV_PATH/bin/activate && python -m yank.main start $extra_args 2>&1 | tee -a $LOG_FILE" Enter

    echo "Session started in detached mode. Use './run.sh attach' to connect."
}

# Function to attach to the session
attach_session() {
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is not running. Use './run.sh start' to start it."
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

# Function to stop the session
stop_session() {
    local stopped=false

    # First check if port is in use and kill that process
    if is_port_in_use; then
        local pid=$(get_pid_by_port)
        if [ -n "$pid" ]; then
            echo "Stopping clipboard-sync (PID: $pid)..."
            kill "$pid" 2>/dev/null
            sleep 1

            # Force kill if still running
            if kill -0 "$pid" 2>/dev/null; then
                echo "Force killing process..."
                kill -9 "$pid" 2>/dev/null
            fi
            stopped=true
        fi
    fi

    # Also kill tmux session if exists
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME"
        stopped=true
    fi

    if $stopped; then
        echo "Session '$SESSION_NAME' stopped."
    else
        echo "Session '$SESSION_NAME' is not running."
        return 1
    fi
}

# Function to restart the session
# Usage: restart_session [--peer IP] [--verbose] [--no-security]
restart_session() {
    echo "Restarting session '$SESSION_NAME'..."
    stop_session 2>/dev/null
    sleep 1
    start_session "$@"
}

# Function to show status
show_status() {
    echo "=== Session Status ==="
    if is_running; then
        echo "[OK] Session '$SESSION_NAME' is running"

        local pid=$(get_pid_by_port)
        if [ -n "$pid" ]; then
            echo "  PID: $pid"
            echo "  Port: $PORT"
        fi

        if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
            echo ""
            echo "Tmux session:"
            tmux list-sessions -F "  #{session_name}: #{session_windows} window(s), #{session_attached} attached" 2>/dev/null | grep "$SESSION_NAME"
        fi
    else
        echo "[X] Session '$SESSION_NAME' is not running"
    fi

    # macOS-specific: show LaunchAgent status
    if $IS_MACOS; then
        echo ""
        echo "=== Auto-Start Status ==="
        if [ -f "$PLIST_PATH" ]; then
            if launchctl list 2>/dev/null | grep -q "$PLIST_NAME"; then
                echo "[OK] LaunchAgent is installed and loaded (will start on boot)"
            else
                echo "[!] LaunchAgent is installed but not loaded"
            fi
        else
            echo "[X] LaunchAgent is not installed (use './run.sh install' to enable auto-start)"
        fi
    fi

    # Linux-specific: show systemd service status
    if $IS_LINUX; then
        echo ""
        echo "=== Auto-Start Status ==="
        if [ -f "$SERVICE_PATH" ]; then
            if systemctl --user is-enabled "$SERVICE_NAME" 2>/dev/null | grep -q "enabled"; then
                echo "[OK] systemd service is installed and enabled (will start on login)"
            else
                echo "[!] systemd service is installed but not enabled"
            fi
        else
            echo "[X] systemd service is not installed (use './run.sh install' to enable auto-start)"
        fi
    fi
}

# macOS-specific: Function to install LaunchAgent
install_launchagent() {
    if ! $IS_MACOS; then
        echo "This command is only available on macOS."
        return 1
    fi

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
        <string>$SCRIPT_DIR/run.sh</string>
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
        echo "[OK] LaunchAgent installed and loaded successfully"
        echo "  clipboard-sync will now start automatically on login"
    else
        echo "[X] Failed to load LaunchAgent"
        return 1
    fi
}

# macOS-specific: Function to uninstall LaunchAgent
uninstall_launchagent() {
    if ! $IS_MACOS; then
        echo "This command is only available on macOS."
        return 1
    fi

    echo "Removing LaunchAgent..."

    if [ ! -f "$PLIST_PATH" ]; then
        echo "LaunchAgent is not installed."
        return 1
    fi

    # Unload the LaunchAgent
    launchctl unload "$PLIST_PATH" 2>/dev/null

    # Remove the plist file
    rm -f "$PLIST_PATH"

    echo "[OK] LaunchAgent removed successfully"
    echo "  clipboard-sync will no longer start automatically on login"
}

# Linux-specific: Function to install systemd user service
install_systemd_service() {
    if ! $IS_LINUX; then
        echo "This command is only available on Linux."
        return 1
    fi

    echo "Installing systemd user service for auto-start..."

    # Create systemd user directory if it doesn't exist
    mkdir -p "$HOME/.config/systemd/user"

    # Create the service file
    cat > "$SERVICE_PATH" << EOF
[Unit]
Description=Yank - LAN Clipboard Sync
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/run.sh start
Restart=on-failure
RestartSec=10
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus

[Install]
WantedBy=default.target
EOF

    # Reload systemd user daemon
    systemctl --user daemon-reload

    # Enable the service
    if systemctl --user enable "$SERVICE_NAME"; then
        echo "[OK] systemd service installed and enabled successfully"
        echo "  clipboard-sync will now start automatically on login"
        echo ""
        echo "To start now, run: systemctl --user start $SERVICE_NAME"
    else
        echo "[X] Failed to enable systemd service"
        return 1
    fi
}

# Linux-specific: Function to uninstall systemd user service
uninstall_systemd_service() {
    if ! $IS_LINUX; then
        echo "This command is only available on Linux."
        return 1
    fi

    echo "Removing systemd user service..."

    if [ ! -f "$SERVICE_PATH" ]; then
        echo "systemd service is not installed."
        return 1
    fi

    # Stop the service if running
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null

    # Disable the service
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null

    # Remove the service file
    rm -f "$SERVICE_PATH"

    # Reload systemd user daemon
    systemctl --user daemon-reload

    echo "[OK] systemd service removed successfully"
    echo "  clipboard-sync will no longer start automatically on login"
}

# Function to start pairing mode
start_pairing() {
    echo "Starting pairing mode..."
    source "$VENV_PATH/bin/activate" && python -m yank.main pair
}

# Function to join/pair with another device
join_device() {
    local host="$1"
    local pin="$2"

    if [ -z "$host" ]; then
        echo "Error: Host IP is required. Usage: ./run.sh join <IP> <PIN>"
        exit 1
    fi

    if [ -z "$pin" ]; then
        echo "Error: PIN is required. Usage: ./run.sh join <IP> <PIN>"
        exit 1
    fi

    echo "Connecting to $host..."
    source "$VENV_PATH/bin/activate" && python -m yank.main join "$host" "$pin"
}

# Function to remove pairing
remove_pairing() {
    source "$VENV_PATH/bin/activate" && python -m yank.main unpair
}

# Function to show security status
show_security() {
    source "$VENV_PATH/bin/activate" && python -m yank.main status
}

# Function to show/edit configuration
show_config() {
    if [ "$1" = "--reset" ]; then
        source "$VENV_PATH/bin/activate" && python -m yank.main config --reset
    elif [ "$1" = "--set" ] && [ -n "$2" ] && [ -n "$3" ]; then
        source "$VENV_PATH/bin/activate" && python -m yank.main config --set "$2" "$3"
    else
        source "$VENV_PATH/bin/activate" && python -m yank.main config
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
        if $IS_MACOS; then
            install_launchagent
        elif $IS_LINUX; then
            install_systemd_service
        else
            echo "Auto-start installation is only supported on macOS and Linux."
        fi
        ;;
    uninstall)
        if $IS_MACOS; then
            uninstall_launchagent
        elif $IS_LINUX; then
            uninstall_systemd_service
        else
            echo "Auto-start uninstallation is only supported on macOS and Linux."
        fi
        ;;
    help|--help|-h)
        show_help
        ;;
    --*)
        # Handle flags passed directly (./run.sh --verbose)
        start_session "$@"
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
