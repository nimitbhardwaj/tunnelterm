# Hermes Web Terminal

Web-based terminal interface with PTY support.

## Installation

### Using uvx (no install required)

```bash
uvx hermes-web-terminal
```

### Using uv run

```bash
uv run hermes-web-terminal
```

### As an installed package

```bash
uv pip install hermes-web-terminal
hermes-web-terminal
```

## Usage

### Basic Usage

Start the server with default settings (binds to `127.0.0.1:4200`):

```bash
uvx hermes-web-terminal
```

### Running Custom Commands

By default, `hermes-web-terminal` runs the `hermes` command. You can specify a different command:

```bash
# Run bash instead of hermes
uvx hermes-web-terminal --command bash

# Run any other CLI tool
uvx hermes-web-terminal --command htop
uvx hermes-web-terminal --command "ls -la"
```

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--host` | Host to bind to | `127.0.0.1` |
| `--port` | Port to bind to | `4200` |
| `--command` | Command to run in the PTY | `hermes` |
| `--password-env` | Env var name containing password | `HERMES_WEB_TERMINAL_PASSWORD` |
| `--config` | Path to config TOML file | `~/.config/hermes-web-terminal/config.toml` |
| `--version` | Show version and exit | - |

### Accessing the Terminal

1. Open your browser to `http://localhost:4200`
2. Enter your password to authenticate
3. Use the terminal as you would a regular PTY

## Configuration

### Config File

Create `~/.config/hermes-web-terminal/config.toml`:

```toml
# Password for authentication (REQUIRED)
password = "your_secure_password_here"

# Optional: Override default host (default: 127.0.0.1)
host = "127.0.0.1"

# Optional: Override default port (default: 4200)
port = 4200

# Optional: Command to run in the PTY (default: hermes)
command = "hermes"
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HERMES_WEB_TERMINAL_PASSWORD` | Password for authentication | (none - must be set via config or env) |

**Note:** Environment variables take precedence over config file values.

## Systemd Service

### Installation

1. Copy the service file:
```bash
sudo cp systemd/hermes-web-terminal.service /etc/systemd/system/
```

2. Create the environment file:
```bash
sudo cp systemd/env.example /etc/hermes-web-terminal/env
sudo chmod 600 /etc/hermes-web-terminal/env
sudo nano /etc/hermes-web-terminal/env  # Set your password
```

3. Customize the service file paths as needed (see Configuration section)

4. Reload and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable hermes-web-terminal
sudo systemctl start hermes-web-terminal
```

### Configuration

The service file is configured with example paths. Update `/etc/systemd/system/hermes-web-terminal.service`:

- `User`: Set to your deployment user
- `WorkingDirectory`: Set appropriately
- `ExecStart`: Update path if `uv` is installed somewhere else

### Checking Status

```bash
sudo systemctl status hermes-web-terminal
sudo journalctl -u hermes-web-terminal -f
```

### Restart Policy

Configured with `Restart=always` and `RestartSec=5` for automatic recovery on failure.
