#!/bin/bash

# Configuration
SESSION_NAME="clipboard-sync"
VENV_PATH="venv"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/clipboard-sync.log"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Function to display help
show_help() {
    cat << EOF
Usage: ./run.sh [COMMAND]

Process Commands:
  start       Start the tmux session in detached mode (default)
  attach      Attach to running session
  logs        View the log file
  tail        Follow log file in real-time
  restart     Kill and restart the session
  stop        Stop the running session
  status      Show session status

Security Commands:
  pair              Enter pairing mode (display PIN)
  join <IP> <PIN>   Pair with another device
  unpair            Remove current pairing
  security          Show security/pairing status

Configuration:
  config                     Show current configuration
  config --set KEY VALUE     Set a configuration value
  config --reset             Reset to defaults

Other:
  help        Show this help message

Examples:
  ./run.sh pair                          # Display PIN for pairing
  ./run.sh join 192.168.1.5 123456       # Pair with device
  ./run.sh start                         # Start syncing
  ./run.sh config                        # View settings
  ./run.sh config --set sync_text false  # Disable text sync

Files:
  Config:  sync_config.json
  Ignore:  .syncignore

EOF
}

# Function to start the session
start_session() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' already running. Use 'attach' to connect or 'restart' to restart."
        return 1
    fi
    
    echo "Creating new tmux session '$SESSION_NAME'..."
    
    # Create new tmux session in detached mode
    tmux new-session -d -s "$SESSION_NAME" -c "$(pwd)"
    
    # Activate venv and run the Python module with logging
    tmux send-keys -t "$SESSION_NAME" "source $VENV_PATH/bin/activate && python -m main start 2>&1 | tee -a $LOG_FILE" Enter
    
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

# Function to restart the session
restart_session() {
    echo "Restarting session '$SESSION_NAME'..."
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME"
        echo "Old session killed."
    fi
    sleep 1
    start_session
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
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "✓ Session '$SESSION_NAME' is running"
        echo ""
        echo "Session details:"
        tmux list-sessions -F "#{session_name}: #{session_windows} window(s), #{session_attached} attached"
    else
        echo "✗ Session '$SESSION_NAME' is not running"
    fi
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
        echo "Error: Host IP is required. Usage: ./run.sh join <IP> <PIN>"
        exit 1
    fi

    if [ -z "$pin" ]; then
        echo "Error: PIN is required. Usage: ./run.sh join <IP> <PIN>"
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
        start_session
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
        restart_session
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
    help|--help|-h)
        show_help
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
