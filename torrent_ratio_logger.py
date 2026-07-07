"""Daily ratio logger: records each torrent's ratio (one entry per day) into
torrent_ratio_log.json so the cleanup script can compute average weekly ratio
change. Intended to be run once per day from cron.
"""

import os
import sys
import json
from datetime import datetime
from typing import Dict, List, Any, Tuple, Set
from contextlib import contextmanager

import requests

import logger_utils
import torrent_utils

# Constants
SECONDS_PER_DAY = 24 * 3600


@contextmanager
def api_session(config, logger):
    """Create, authenticate, and clean up an API session.

    Uses stateless API key auth when [login] api_key is set (qBittorrent
    >= 5.2.0); otherwise falls back to cookie-based username/password login.
    """
    session = requests.Session()
    try:
        if not torrent_utils.setup_session_auth(session, config):
            torrent_utils.login_to_qbittorrent(session,
                                               config.get('login', 'address'),
                                               config.get('login', 'username'),
                                               config.get('login', 'password'), logger)
        yield session
    finally:
        session.close()


def load_existing_data(file_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Load existing data from the log file.

    Unlike torrent_utils.load_ratio_log, a corrupt file raises here instead of
    returning {}: silently starting fresh would wipe the accumulated history
    on the next save.
    """
    try:
        with open(file_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding JSON from {file_path}: {e}")


def save_data(file_path: str, data: Dict[str, List[Dict[str, Any]]], logger: Any) -> None:
    """Save data to the log file."""
    try:
        with open(file_path, 'w') as file:
            json.dump(data, file, indent=4)
    except Exception as e:
        logger.error(f"Error saving ratio log file: {e}")


def process_torrent_data(torrents: List[Dict[str, Any]], old_data: Dict[str, List[Dict[str, Any]]],
                         max_entries: int, purge_days: List[int]) -> Tuple[Dict[str, List[Dict[str, Any]]], Set[str]]:
    """Process torrent data and update the log.

    Builds the new log from scratch, so torrents that no longer exist in the
    client (e.g. removed manually) drop out of the file automatically.
    """
    new_data: Dict[str, List[Dict[str, Any]]] = {}
    current_date = datetime.now().strftime('%Y-%m-%d')
    current_hashes: Set[str] = set()

    for torrent in torrents:
        torrent_hash = torrent['hash']
        current_hashes.add(torrent_hash)
        seed_days = torrent['seeding_time'] // SECONDS_PER_DAY
        ratio_record = {'date': current_date, 'ratio': torrent['ratio']}

        if torrent_hash not in old_data:
            new_data[torrent_hash] = [ratio_record]
        else:
            entries = old_data[torrent_hash]
            if not entries or entries[-1]['date'] != current_date:
                entries.append(ratio_record)
                if purge_days and seed_days in purge_days and len(entries) > 1:
                    entries.pop(0)

            entries = entries[-max_entries:]
            new_data[torrent_hash] = entries

    return new_data, current_hashes


def log_statistics(new_data: Dict[str, List[Dict[str, Any]]], old_hashes: Set[str],
                   current_hashes: Set[str], logger: Any, max_entries: int) -> None:
    total_torrents = len(current_hashes)
    new_torrents_added = len(current_hashes - old_hashes)
    torrents_removed = len(old_hashes - current_hashes)
    torrents_with_max_entries = sum(1 for entries in new_data.values() if len(entries) >= max_entries)

    logger.info(f"Total torrents in log: {total_torrents}, "
                f"New torrents added: {new_torrents_added}, "
                f"Torrents removed: {torrents_removed}, "
                f"Torrents with max entries: {torrents_with_max_entries}")


def update_ratio_log(config, log_file_path: str, logger: Any, max_entries: int,
                     purge_days: List[int]) -> bool:
    """Update the ratio log. Returns True on success, False on failure."""
    api_address = config.get('login', 'address')
    try:
        with api_session(config, logger) as session:
            torrents = torrent_utils.get_torrent_list(session, api_address, logger)
            old_data = load_existing_data(log_file_path)

            # Get the current set of torrent hashes before processing
            old_hashes = set(old_data.keys())

            new_data, current_hashes = process_torrent_data(torrents, old_data, max_entries, purge_days)
            save_data(log_file_path, new_data, logger)

            log_statistics(new_data, old_hashes, current_hashes, logger, max_entries)
        return True
    except Exception as e:
        logger.error(f"Failed to update ratio log: {e}")
        return False


if __name__ == "__main__":
    script_directory = os.path.dirname(os.path.abspath(__file__))
    config = torrent_utils.load_configuration(script_directory)

    logger, log_handler = logger_utils.setup_logger(config=config)

    log_file_path = os.path.join(script_directory, 'torrent_ratio_log.json')

    max_entries = config.getint('torrent_ratio_logger', 'max_entries', fallback=28)
    purge_days_str = config.get('torrent_ratio_logger', 'purge_days', fallback='')
    purge_days = [int(day.strip()) for day in purge_days_str.split(',') if day.strip()]

    logger.info("Running torrent ratio logger script")
    try:
        success = update_ratio_log(config, log_file_path, logger, max_entries, purge_days)
    finally:
        # Flush buffered log entries even if the update failed or crashed.
        log_handler.write_log_entries()
    sys.exit(0 if success else 1)
