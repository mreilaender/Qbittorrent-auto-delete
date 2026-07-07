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
- Detailed logging of actions and decisions (newest run at the top of the log)

## Requirements

- Python 3.8+
- qBittorrent with Web UI enabled (any recent version; API key auth requires ≥ 5.2.0)
- `requests` library

## Installation and Usage

1. Clone this repository
2. Install the required Python packages: `pip install -r requirements.txt`
3. Copy `config.example.ini` to `config.ini` and edit it (every setting is documented in the file)
4. Run the script: `python main.py`

To run in test mode (no actual deletions), use: `python main.py --test`

## Authentication

Two ways to let the scripts talk to qBittorrent, configured in the `[login]` section:

**API key (recommended, qBittorrent ≥ 5.2.0):** in the qBittorrent WebUI, open Options (gear icon) → Web UI → API key section → click Generate, and paste the `qbt_...` key into `api_key` in `config.ini`. Username and password can then be left empty. Note that qBittorrent supports a single key, and regenerating it immediately invalidates the old one - update the config when you rotate it.

**Username/password (any version):** leave `api_key` empty and fill in `username` and `password` with your Web UI credentials.

## Configuration

The scripts use a `config.ini` file located next to them. See `config.example.ini` for a fully documented template covering seeding rules, bonus rules, the ratio grace period, hardlink checking, and path mapping.

## Files created by the scripts

| File | Purpose |
|---|---|
| `deletelog.txt` | Human-readable log, newest run at the top (rotating, max 3 backups of 1 MB) |
| `torrent_ratio_log.json` | Daily ratio history per torrent, written by `torrent_ratio_logger.py` |
| `ratio_grace_state.json` | Grace-period timestamps, managed automatically (self-cleaning) |

All of these clean themselves up as torrents come and go - including torrents you remove manually.

## Torrent Ratio Logger

A separate module (`torrent_ratio_logger.py`) manages the `torrent_ratio_log.json` file, tracking ratio history of torrents over time. This history is what the removal prioritization is based on, so let it run for a few days before expecting accurate "average weekly ratio" ordering.

## Recommended Usage

1. Run `torrent_ratio_logger.py` once daily.
2. Run `main.py` every hour (or more often).

Note: the ratio grace period clock starts when `main.py` first *observes* a torrent at its ratio target, so the effective grace is between `ratio_grace_seconds` and `ratio_grace_seconds` + your run interval. With hourly runs and the default 3600 s, torrents keep seeding roughly 1-2 hours after hitting their target.

### Automating

Add to your crontab in Linux / User Scripts in Unraid / Task Scheduler in Windows:

    0 0 * * * /usr/bin/python /path/to/your/torrent_ratio_logger.py
    0 * * * * /usr/bin/python /path/to/your/main.py
    @reboot pip install -r /path/to/your/requirements.txt

Both scripts exit with a non-zero exit code on failure, so cron/systemd failure notifications work out of the box.

## Test Mode

Run with the `--test` flag to see potential actions without making changes:

    python main.py --test

The planned removals are written to `deletelog.txt` exactly as a real run would, prefixed with `TEST MODE`. Recommended after every rule change.

---

# Unraid Setup Guide

Follow the steps below to install Python, configure your script, and set up automated tasks.

### Prerequisites
Before starting, ensure you have the following installed on your Unraid system:

- Nerd Tools Addon (for Python 3 installation)
- User Scripts Addon (for managing your scripts)

### Step 1: Edit Your Configuration
Copy `config.example.ini` to `config.ini` and customize it according to your preferences.

### Step 2: Install Required Packages at Startup
To install Python packages automatically at array startup, use the following script:

    #!/bin/bash
    # This script installs pip and required Python packages at boot

    # Check if pip is already installed
    if ! command -v pip3 &> /dev/null
    then
        echo "pip not found, installing..."
        # Download get-pip.py
        curl -s https://bootstrap.pypa.io/get-pip.py -o /boot/config/get-pip.py
        # Install pip
        python3 /boot/config/get-pip.py
    else
        echo "pip already installed"
    fi

    # Install required Python packages
    python3 -m pip install --quiet requests

    echo "Python environment setup complete."

Save this script and configure it to run at array startup using the User Scripts addon.

### Step 3: Set Up Logging
To log torrent ratios daily, use the following script. Schedule it to run daily at 00:01:

    #!/bin/bash
    python3 /mnt/path/torrent_ratio_logger.py

Set up a cron job with the following timing:

    1 0 * * *

This configuration runs the script every day at 00:01 AM.

### Step 4: Run the Main Script

    #!/bin/bash
    python3 /mnt/path/main.py

Set up a cron job with the following timing or whatever you would like:

    15 * * * *

This configuration runs the script every hour at 15 minutes past.

### Step 5: Test Mode
To test changes without making actual deletions, add the `--test` flag:

    python3 /mnt/path/main.py --test

This simulates the actions, and `deletelog.txt` will show what the script would have done without making any real changes.
