# Qbittorrent-auto-delete

Tired of managing complex seeding rules and manually selecting torrents to remove when your disk space runs low? This script is designed to simplify your life. Once set up, you only need to define seeding rules for each category. Then, as you add new torrents to these categories, they'll be managed automatically.

When your drive starts to fill up, the script takes action. It prioritizes removing the least performing torrents - those with the lowest seeding ratio over the past month - until it reaches your specified free disk space or torrent count.

With this tool, you can:

- Automate torrent management
- Maintain optimal disk space
- Ensure your best-performing torrents keep seeding

Say goodbye to manual torrent management and hello to a more efficient, hands-off approach!

If you found this script useful, you can [![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-☕-yellow.svg)](https://www.buymeacoffee.com/Mythic82)

## Overview

This script automates the process of maintaining your qBittorrent instance by:

1. Deleting torrents that have fulfilled specified seeding rules.
2. Freeing up disk space when a set minimum free space threshold is reached.
3. Limiting the number of seeding torrents in specified categories.

Torrents are prioritized for deletion based on their seeding ratio over the last month, with those having the lowest ratio being removed first.

## Features

- Configurable seeding rules per torrent category (seed time and/or ratio)
- Minimum free space threshold for disk management, including space reserved for unfinished downloads
- Maximum torrent count limit per category
- **Ratio grace period**: torrents that hit their ratio target keep seeding a configurable extra period before removal, so the tracker has time to register the final ratio via regular announces (protects small torrents that hit their ratio within minutes)
- Bonus multipliers for long-term seeding and large torrents, so your best-earning torrents are removed last
- Optional hardlink protection: skip torrents whose files are hardlinked elsewhere (deleting them would free no space anyway)
- **API key authentication** (qBittorrent ≥ 5.2.0) with username/password fallback for older versions
- Test mode for safe execution without actual deletions
- Detailed logging to stdout with tag-based separation ([cleanup], [scheduler])

## Requirements

- Python 3.8+ (for direct execution)
- qBittorrent with Web UI enabled (any recent version; API key auth requires ≥ 5.2.0)
- `requests` library (auto-installed via pip)

## Installation

### Via pip (recommended)

```bash
pip install .
```

This installs two commands: `qbittorrent-cleanup` (single run) and `qbittorrent-scheduler` (continuous loop).

### Direct execution

```bash
python main.py [--test]
python scheduler.py
```

## Configuration

The scripts use a `config.ini` file. Resolve the path in this order:

1. `CONFIG_PATH` environment variable
2. `--config` CLI flag (both commands)
3. `STATE_DIR/config.ini` (default: current directory)

See `config.ini` for a fully documented template covering seeding rules, bonus rules, the ratio grace period, hardlink checking, and path mapping.

## Authentication

Two ways to let the scripts talk to qBittorrent, configured in the `[login]` section:

**API key (recommended, qBittorrent ≥ 5.2.0):** in the qBittorrent WebUI, open Options (gear icon) → Web UI → API key section → click Generate, and paste the `qbt_...` key into `api_key` in `config.ini`. Username and password can then be left empty. Note that qBittorrent supports a single key, and regenerating it immediately invalidates the old one - update the config when you rotate it.

**Username/password (any version):** leave `api_key` empty and fill in `username` and `password` with your Web UI credentials.

## Commands

### Cleanup

Removes torrents based on configured rules:

```bash
qbittorrent-cleanup --config /path/to/config.ini
qbittorrent-cleanup --test          # preview only
python main.py --test               # direct execution
```

### Scheduler

Runs cleanup at a configurable interval, optionally logging torrent ratios daily:

```bash
qbittorrent-scheduler
qbittorrent-scheduler --cleanup-interval 2   # every 2 hours
qbittorrent-scheduler --no-cleanup           # only ratio log (no cleanup)
qbittorrent-scheduler --config /path/to/config.ini
```

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_INTERVAL` | `1` | Cleanup interval in hours |
| `CONFIG_PATH` | *(see resolution order above)* | Path to config.ini |
| `STATE_DIR` | `.` (script directory) | Directory for JSON state files |

The scheduler handles graceful shutdown on SIGTERM/SIGINT: it sets a flag, drains pending jobs, then exits.

## Logging

All output goes to **stdout** with tag-based prefixes so multiple modules are distinguishable:

```
2025-01-15 03:00:00 [cleanup] INFO     Free: 12.34 GB, DLremain: 45.6 GB, Diskneed: 237 GB
2025-01-15 03:00:01 [cleanup] INFO     MyTorrent.mkv       EX1   250.00 GB     45.2 Weeks     0.012 R/W
2025-01-15 03:00:01 [scheduler] INFO   Cleanup job scheduled every 1 hour(s)
```

- `[cleanup]` — cleanup job output
- `[scheduler]` — scheduler lifecycle messages

Log level is set in `config.ini` under `[logging] log_level` (DEBUG, INFO, WARNING, ERROR).

## Files created by the scripts

| File | Purpose |
|---|---|
| `torrent_ratio_log.json` | Daily ratio history per torrent (if ratio log job runs) |
| `ratio_grace_state.json` | Grace-period timestamps, managed automatically (self-cleaning) |

All JSON state files are written to the directory resolved by `STATE_DIR`.

## Test Mode

Run with the `--test` flag to see potential actions without making changes:

```bash
qbittorrent-cleanup --test
python main.py --test
```

The planned removals are logged to stdout exactly as a real run would, prefixed with `TEST MODE`. Recommended after every rule change.

---

## Running with Docker

### Build

```bash
docker build -t qbittorrent-auto-delete .
```

### Run the scheduler (recommended)

```bash
docker run -d \
  --name qbittorrent-cleanup \
  -v /path/to/config.ini:/app/config.ini \
  qbittorrent-auto-delete
```

All persistent files (config + JSON state) live in the mounted volume. The scheduler runs both the cleanup and ratio log jobs.

### Run cleanup only (ad-hoc)

```bash
docker run --rm \
  -v /path/to/config.ini:/app/config.ini \
  qbittorrent-auto-delete qbittorrent-cleanup --test
```

### Override scheduler settings

```bash
docker run -d \
  --name qbittorrent-cleanup \
  -v /path/to/config.ini:/app/config.ini \
  -e CLEANUP_INTERVAL=2 \
  -e STATE_DIR=/data \
  -v /path/to/data:/data \
  qbittorrent-auto-delete
```

### Environment variables (Docker)

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_INTERVAL` | `1` | Cleanup interval in hours |
| `CONFIG_PATH` | `/app/config.ini` | Path to config.ini inside container |
| `STATE_DIR` | `/app` | Directory for JSON state files |

### Healthcheck

The image includes a built-in Docker healthcheck that runs cleanup in `--test` mode every 5 minutes:

```bash
docker inspect --format='{{.State.Health.Status}}' qbittorrent-cleanup
```

### Docker volume

The container declares `VOLUME ["/app"]`. Mount a bind volume or named volume to `/app` for config and state persistence:

```bash
docker run -d \
  -v qbittorrent-data:/app \
  -v /path/to/config.ini:/app/config.ini \
  qbittorrent-auto-delete
```

## Test Mode

Run with the `--test` flag to see potential actions without making changes:

```bash
python main.py --test
```

The planned removals are written to stdout exactly as a real run would, prefixed with `TEST MODE`. Recommended after every rule change.
