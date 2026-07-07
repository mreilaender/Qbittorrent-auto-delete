"""qBittorrent cleanup: removes seeded-out torrents to maintain free disk
space and per-category torrent counts, according to rules in config.ini.

Usage:
    python main.py [--test]

--test logs what would be removed without actually removing anything.
"""

import sys
import os
from typing import List, Dict, Any
from logging import Logger
from configparser import ConfigParser

import requests

import logger_utils
import torrent_utils


def check_space_and_remove_torrents(session: requests.Session, logger: Logger, config: ConfigParser,
                                    test_mode: bool, bonus_rules: Dict[str, Dict[str, Any]]) -> None:
    api_address = config.get('login', 'address')
    download_minspace_gb_raw = config.get('cleanup', 'download_minspace_gb', fallback='')
    min_space_gb = config.getfloat('cleanup', 'min_space_gb')
    categories_space = torrent_utils.parse_category_list(
        config.get('cleanup', 'categories_to_check_for_space', fallback=''))
    categories_count = torrent_utils.parse_category_list(
        config.get('cleanup', 'categories_to_check_for_number', fallback=''))

    script_directory = os.path.dirname(os.path.abspath(__file__))
    configured_drive_path = config.get('cleanup', 'drive_path', fallback='').strip()
    drive_path = configured_drive_path if configured_drive_path else script_directory

    free_space = torrent_utils.get_free_space(drive_path)

    # Load the ratio log once; it is passed around as a dict instead of being
    # re-read from disk for every torrent.
    ratio_log = torrent_utils.load_ratio_log(
        os.path.join(script_directory, 'torrent_ratio_log.json'))

    try:
        all_torrents = torrent_utils.get_torrent_list(session, api_address, logger)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code not in (401, 403):
            raise
        if 'Authorization' in session.headers:
            # API key auth is stateless; a 401/403 means the key itself was
            # rejected. Re-login is not possible (auth endpoints reject keys).
            raise torrent_utils.QBittorrentError(
                f"qBittorrent rejected the API key (HTTP {e.response.status_code}). "
                f"Check the key in config.ini and that qBittorrent is v5.2.0 or newer.")
        # Cookie auth: session expired or not authenticated yet - log in, retry once.
        torrent_utils.login_to_qbittorrent(session, api_address,
                                           config.get('login', 'username'),
                                           config.get('login', 'password'), logger)
        all_torrents = torrent_utils.get_torrent_list(session, api_address, logger)

    # Estimate disk space still needed by unfinished downloads. Includes
    # stalled/queued/forced downloads, not just actively transferring ones.
    downloading_torrents = [t for t in all_torrents
                            if t['state'] in torrent_utils.ACTIVE_DOWNLOAD_STATES]
    total_remaining_size_gb = sum(t['size'] * (1 - t['progress']) for t in downloading_torrents) / (1024 ** 3)
    space_left_after_downloads = free_space - total_remaining_size_gb

    # Check if download_minspace_gb is set and not empty
    if download_minspace_gb_raw and download_minspace_gb_raw.strip():
        download_minspace_gb = float(download_minspace_gb_raw)
        additional_space_needed = max(0.0, download_minspace_gb - space_left_after_downloads)
    else:
        additional_space_needed = 0.0

    space_needed = max(0.0, min_space_gb - free_space)

    category_rules = torrent_utils.get_category_rules(config)
    filtered_torrents = torrent_utils.filter_torrents_by_rules(
        all_torrents,
        category_rules,
        logger,
        config,
        os.path.join(script_directory, 'ratio_grace_state.json')
    )

    # Only process if there's work to be done
    if space_needed > 0 or additional_space_needed > 0:
        torrents_removed_by_space = torrent_utils.remove_torrents_by_space(
            filtered_torrents,
            categories_space,
            max(additional_space_needed, space_needed),
            logger,
            session,
            api_address,
            test_mode,
            ratio_log,
            bonus_rules,
            config
        )
    else:
        torrents_removed_by_space = []

    # Exclude torrents already removed in the space phase so a category that
    # appears in both lists can't select (and log) the same torrent twice.
    removed_hashes = {t['hash'] for t in torrents_removed_by_space}

    if categories_count:
        remaining_all = [t for t in all_torrents if t['hash'] not in removed_hashes]
        remaining_eligible = [t for t in filtered_torrents if t['hash'] not in removed_hashes]
        torrents_removed_by_count = torrent_utils.remove_torrents_by_count(
            remaining_all,
            remaining_eligible,
            categories_count,
            config.getint('cleanup', 'max_torrents_for_categories'),
            logger,
            session,
            api_address,
            test_mode,
            ratio_log,
            bonus_rules,
            config.getboolean('cleanup', 'sort_count_removal_by_size', fallback=False),
            config
        )
    else:
        torrents_removed_by_count = []

    all_removed_torrents = torrents_removed_by_space + torrents_removed_by_count

    # Only log if something was actually removed
    if all_removed_torrents:
        log_removal_info(logger, free_space, total_remaining_size_gb, space_needed,
                         additional_space_needed, all_removed_torrents, test_mode,
                         bonus_rules, config, ratio_log)


def log_removal_info(logger: Logger, free_space: float, total_remaining_size_gb: float,
                     space_needed: float, additional_space_needed: float,
                     all_removed_torrents: List[Dict[str, Any]], test_mode: bool,
                     bonus_rules: Dict[str, Dict[str, Any]], config: ConfigParser,
                     ratio_log: Dict[str, List[Dict[str, Any]]]) -> None:
    """Log information about removed or would-be removed torrents."""
    logger.info(f"{'TEST MODE: ' if test_mode else ''}Free: {free_space:.2f} GB, "
                f"DLremain: {total_remaining_size_gb:.1f} GB, "
                f"Diskneed: {max(space_needed, additional_space_needed):.0f} GB")
    logger_utils.log_torrent_removal_info(all_removed_torrents, logger, bonus_rules, config, ratio_log)


def main(test_mode: bool, logger: Logger, handler: Any, config: ConfigParser,
         session: requests.Session) -> int:
    """Run the cleanup. Returns a process exit code."""
    exit_code = 0
    try:
        bonus_rules = torrent_utils.load_bonus_rules(config)
        check_space_and_remove_torrents(session, logger, config, test_mode, bonus_rules)
    except torrent_utils.QBittorrentError as e:
        logger.error(str(e))
        exit_code = 1
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        exit_code = 1
    finally:
        # Always flush buffered log entries, even after a login failure.
        handler.write_log_entries()
    return exit_code


if __name__ == "__main__":
    script_directory = os.path.dirname(os.path.abspath(__file__))
    config = torrent_utils.load_configuration(script_directory)
    logger, log_handler = logger_utils.setup_logger(config=config)
    session = requests.Session()
    torrent_utils.setup_session_auth(session, config)  # API key auth if configured
    test_mode = '--test' in sys.argv
    try:
        sys.exit(main(test_mode, logger, log_handler, config, session))
    finally:
        session.close()
