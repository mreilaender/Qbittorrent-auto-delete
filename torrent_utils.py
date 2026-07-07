"""Shared utilities for the qBittorrent cleanup and ratio-logging scripts."""

import os
import time
import json
import configparser
from shutil import disk_usage
from typing import Dict, List, Any, Optional, Tuple
from logging import Logger

import requests

# Constants
API_V2_BASE = "/api/v2"
BYTES_TO_GB = 1024 ** 3
SECONDS_PER_WEEK = 7 * 86400

# qBittorrent states in which a torrent is still going to consume disk space.
# Paused downloads are deliberately excluded (they may never resume).
ACTIVE_DOWNLOAD_STATES = {'downloading', 'stalledDL', 'metaDL', 'queuedDL',
                          'forcedDL', 'checkingDL', 'allocating'}


class QBittorrentError(Exception):
    """Raised when a qBittorrent API operation fails (login, requests, etc.).

    Raising instead of calling sys.exit() lets callers run their cleanup
    logic (e.g. flushing buffered log entries) before the process ends.
    """


# ---------------------------------------------------------------------------
# Filesystem / configuration helpers
# ---------------------------------------------------------------------------

def get_drive_path(file_path: str) -> str:
    """Find the mount point of a given file path."""
    file_path = os.path.abspath(file_path)
    while not os.path.ismount(file_path):
        file_path = os.path.dirname(file_path)
    return file_path


def get_free_space(drive_path: str) -> float:
    """Get free space on a given drive in GB."""
    return disk_usage(drive_path).free / BYTES_TO_GB


def load_configuration(script_directory: str) -> configparser.ConfigParser:
    """Load configuration from the config file."""
    config_path = os.path.join(script_directory, 'config.ini')
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def parse_category_list(raw_value: str) -> List[str]:
    """Parse a comma-separated category list, dropping empty entries."""
    return [cat.strip().lower() for cat in raw_value.split(',') if cat.strip()]


# ---------------------------------------------------------------------------
# qBittorrent API
# ---------------------------------------------------------------------------

def setup_session_auth(session: requests.Session, config: configparser.ConfigParser) -> bool:
    """Configure API key authentication on the session if a key is set.

    qBittorrent >= 5.2.0 supports stateless API key authentication: every
    request carries an 'Authorization: Bearer <key>' header and the cookie
    based login/logout flow must be skipped entirely (the auth endpoints
    reject API keys). Returns True if key auth is in use, False if the
    caller should fall back to username/password login.
    """
    api_key = config.get('login', 'api_key', fallback='').strip()
    if api_key:
        session.headers['Authorization'] = f'Bearer {api_key}'
        return True
    return False


def login_to_qbittorrent(session: requests.Session, api_address: str, username: str,
                         password: str, logger: Logger) -> None:
    """Login to the qBittorrent API. Raises QBittorrentError on failure."""
    login_url = f"{api_address}{API_V2_BASE}/auth/login"
    try:
        response = session.post(login_url, data={'username': username, 'password': password})
        response.raise_for_status()
    except requests.RequestException as e:
        raise QBittorrentError(f"Login request failed: {e}") from e

    # qBittorrent < 5.2: HTTP 200 with body 'Ok.' on success, 'Fails.' on bad credentials.
    # qBittorrent >= 5.2: HTTP 204 with empty body on success (WebAPI now returns 204
    # whenever the response contains no data).
    if response.status_code == 204 or response.text == 'Ok.':
        return
    if response.text == 'Fails.':
        raise QBittorrentError("Login failed: invalid username or password")
    raise QBittorrentError(
        f"Login failed: unexpected response (status={response.status_code}, body={response.text!r})")


def get_torrent_list(session: requests.Session, api_address: str, logger: Logger) -> List[Dict[str, Any]]:
    """Get the list of torrents from the qBittorrent API."""
    torrent_list_url = f"{api_address}{API_V2_BASE}/torrents/info"
    response = session.get(torrent_list_url)
    response.raise_for_status()  # Raises HTTPError for bad responses (incl. 403)
    return response.json()


def get_torrent_files(session: requests.Session, api_address: str, torrent_hash: str,
                      logger: Logger) -> List[Dict[str, Any]]:
    """Get the list of files for a specific torrent."""
    files_url = f"{api_address}{API_V2_BASE}/torrents/files"
    try:
        response = session.get(files_url, params={'hash': torrent_hash})
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to get files for torrent {torrent_hash}: {e}")
        return []


def remove_torrent(session: requests.Session, api_address: str, torrent_hash: str,
                   delete_files: bool, logger: Logger) -> bool:
    """Remove a torrent from qBittorrent. Returns True on success."""
    removal_url = f"{api_address}{API_V2_BASE}/torrents/delete"
    data = {'hashes': torrent_hash, 'deleteFiles': str(delete_files).lower()}
    try:
        response = session.post(removal_url, data=data)
        response.raise_for_status()
        logger.debug(f"Torrent {torrent_hash} successfully removed.")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to remove torrent {torrent_hash}: {e}")
        return False


# ---------------------------------------------------------------------------
# Hardlink detection
# ---------------------------------------------------------------------------

def translate_path(qbt_path: str, config: configparser.ConfigParser) -> str:
    """Translate qBittorrent's reported path to the actual filesystem path."""
    old_prefix = config.get('path_mapping', 'qbt_prefix', fallback='/ssd')
    new_prefix = config.get('path_mapping', 'actual_prefix', fallback='/mnt/nvme')

    if qbt_path.startswith(old_prefix):
        return qbt_path.replace(old_prefix, new_prefix, 1)
    return qbt_path


def has_hardlinked_files(torrent: Dict[str, Any], session: requests.Session, api_address: str,
                         logger: Logger, config: configparser.ConfigParser) -> bool:
    """
    Check if any files in the torrent are hardlinked (st_nlink > 1).

    If 'check_hardlinks' in the [cleanup] section is turned off (e.g. 'off',
    'no', 'false', '0'), the check is skipped entirely and this function
    returns False without making any API/filesystem calls.

    Note: this performs one API call plus a stat() per file, so callers
    should invoke it lazily (only for torrents actually about to be removed).
    """
    if not config.getboolean('cleanup', 'check_hardlinks', fallback=True):
        return False

    try:
        save_path = torrent.get('save_path', '')
        if not save_path:
            logger.warning(f"No save path found for torrent: {torrent['name']}")
            return False

        actual_save_path = translate_path(save_path, config)

        files = get_torrent_files(session, api_address, torrent['hash'], logger)
        if not files:
            return False

        for file_info in files:
            file_name = file_info.get('name', '')
            file_path = os.path.join(actual_save_path, file_name)

            if os.path.exists(file_path):
                try:
                    stat_info = os.stat(file_path)
                    if stat_info.st_nlink > 1:
                        logger.debug(f"Hardlinked file detected: {torrent['name'][:60]} "
                                     f"(links: {stat_info.st_nlink})")
                        return True
                except OSError as e:
                    logger.warning(f"Could not stat file {file_path}: {e}")
                    continue

        return False
    except Exception as e:
        logger.error(f"Error checking hardlinks for torrent {torrent['name']}: {e}")
        return False


# ---------------------------------------------------------------------------
# Ratio log / bonus rules
# ---------------------------------------------------------------------------

def load_ratio_log(log_file_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Load the ratio log from file. Call once per run and pass the dict around."""
    try:
        with open(log_file_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from {log_file_path}: {e}")
        return {}


def load_bonus_rules(config: configparser.ConfigParser) -> Dict[str, Dict[str, Any]]:
    """Load bonus rules from config."""
    bonus_rules = {}
    if 'bonus_rules' in config:
        for category, rule_string in config['bonus_rules'].items():
            category_rules = {}
            for rule in rule_string.split(', '):
                key, value = rule.split(':', 1)
                if key in ['min_weeks', 'extra_multiplier_weeks', 'extra_multiplier_value']:
                    category_rules[key] = float(value)
                elif key in ['time_multipliers', 'size_multipliers']:
                    category_rules[key] = parse_multipliers(value)
            if category_rules:  # Only add the category if it has any bonus rules
                bonus_rules[category.lower()] = category_rules
    return bonus_rules


def parse_multipliers(multiplier_string: str) -> List[Tuple[float, float]]:
    """Parse a multiplier string into a list of (threshold, multiplier) tuples."""
    return [(float(pair.split(':')[0]), float(pair.split(':')[1]))
            for pair in multiplier_string.split(',')]


def get_multiplier(value: float, multipliers: List[Tuple[float, float]]) -> float:
    """Get the appropriate multiplier based on the value."""
    for threshold, multiplier in reversed(multipliers):
        if value >= threshold:
            return multiplier
    return 1.0


def apply_bonus_rules(torrent: Dict[str, Any], bonus_rules: Dict[str, Dict[str, Any]],
                      logger: Logger) -> float:
    """Apply bonus rules to calculate the ratio-change multiplier.

    Note: category comparison is case-insensitive. configparser lowercases
    option keys, so comparing against the raw torrent category (e.g. 'SB')
    silently disabled bonus rules in the original implementation.
    """
    torrent_category = torrent.get('category', '').lower()
    weeks_seeded = torrent.get('seeding_time', 0) / SECONDS_PER_WEEK
    torrent_size = torrent.get('size', 0)

    if torrent_category in bonus_rules:
        category_rules = bonus_rules[torrent_category]
        logger.debug(f"{torrent_category} category adjustments for torrent: {torrent['name']}")

        multiplier = 1.0

        if 'time_multipliers' in category_rules:
            multiplier *= get_multiplier(weeks_seeded, category_rules['time_multipliers'])

        if 'size_multipliers' in category_rules:
            multiplier *= get_multiplier(torrent_size / BYTES_TO_GB, category_rules['size_multipliers'])

        if 'extra_multiplier_weeks' in category_rules and 'extra_multiplier_value' in category_rules:
            if weeks_seeded >= category_rules['extra_multiplier_weeks']:
                multiplier *= category_rules['extra_multiplier_value']

        return multiplier

    return 1.0


def calculate_average_ratio(torrent: Dict[str, Any], ratio_log: Dict[str, List[Dict[str, Any]]],
                            logger: Logger, bonus_rules: Dict[str, Dict[str, Any]],
                            config: configparser.ConfigParser) -> float:
    """Calculate the bonus-adjusted average weekly ratio change for a torrent.

    `ratio_log` is the already-loaded ratio log dict (see load_ratio_log);
    loading it once per run instead of once per torrent avoids re-reading and
    re-parsing the same JSON file hundreds of times.
    """
    ratio_records = ratio_log.get(torrent['hash'], [])
    current_ratio = torrent['ratio']
    ratio_old = ratio_records[0]['ratio'] if ratio_records else None
    weeks_seeded = torrent.get('seeding_time', 0) / SECONDS_PER_WEEK
    num_records_weeks = len(ratio_records) / 7

    min_ratio_change = config.getfloat('ratio_calculation', 'min_ratio_change', fallback=0.3)
    min_weeks_seeded = config.getfloat('ratio_calculation', 'min_weeks_seeded', fallback=3)

    if ratio_old is not None:
        ratio_change = current_ratio - ratio_old
        if min_weeks_seeded > 0:
            ratio_change = max(ratio_change, min_ratio_change) if num_records_weeks <= min_weeks_seeded else ratio_change
        average_ratio_change = ratio_change / num_records_weeks if ratio_change != 0 and num_records_weeks > 0 else 0
    elif min_ratio_change > 0 and min_weeks_seeded > 0 and current_ratio < min_ratio_change and weeks_seeded <= min_weeks_seeded:
        average_ratio_change = min_ratio_change / weeks_seeded if weeks_seeded > 0 else 0
    else:
        average_ratio_change = current_ratio / weeks_seeded if weeks_seeded > 0 else 0

    average_ratio_change *= apply_bonus_rules(torrent, bonus_rules, logger)

    return average_ratio_change


# ---------------------------------------------------------------------------
# Ratio grace period state
# ---------------------------------------------------------------------------

def load_ratio_grace_state(state_file_path: str) -> Dict[str, float]:
    """Load the ratio grace state file (torrent hash -> unix timestamp when
    the ratio requirement was first observed as met)."""
    try:
        with open(state_file_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Corrupt state file: start fresh rather than crashing the cleanup run
        return {}


def save_ratio_grace_state(state_file_path: str, state: Dict[str, float], logger: Logger) -> None:
    """Save the ratio grace state file."""
    try:
        with open(state_file_path, 'w') as file:
            json.dump(state, file, indent=4)
    except Exception as e:
        logger.error(f"Error saving ratio grace state file: {e}")


# ---------------------------------------------------------------------------
# Rule filtering
# ---------------------------------------------------------------------------

def get_category_rules(config: configparser.ConfigParser) -> Dict[str, Dict[str, float]]:
    """Get seed time / ratio / grace rules for each category."""
    rules = {}
    for category, rule_string in config['seed_rules'].items():
        category_rules = {}
        for rule in rule_string.split(', '):
            key, value = rule.split(':')
            if key in ['min_seed_time', 'min_ratio', 'ratio_grace']:
                category_rules[key] = float(value)
        if category_rules:  # Only add the category if it has any rules
            rules[category.lower()] = category_rules
    return rules


def filter_torrents_by_rules(torrents: List[Dict[str, Any]], category_rules: Dict[str, Dict[str, float]],
                             logger: Logger, config: Optional[configparser.ConfigParser] = None,
                             grace_state_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return torrents eligible for removal according to the per-category rules.

    Ratio grace period: when a torrent becomes eligible purely because it hit
    its ratio target (and not its seed time), keep seeding it for a while so
    the tracker has time to register the final ratio via regular announces.
    """
    filtered_torrents = []
    categories_seen = set()

    default_grace = config.getfloat('cleanup', 'ratio_grace_seconds', fallback=0.0) if config else 0.0
    grace_state = load_ratio_grace_state(grace_state_path) if grace_state_path else {}
    grace_state_changed = False
    now = time.time()
    current_hashes = set()
    held_back_count = 0

    for torrent in torrents:
        current_hashes.add(torrent['hash'])
        category = torrent.get('category', '').lower()

        if category not in categories_seen:
            categories_seen.add(category)
            if category in category_rules:
                logger.debug(f"Category '{category}' has rules: {category_rules[category]}")
            else:
                logger.debug(f"No rules configured for category: '{category}'")

        if category not in category_rules:
            continue

        rules = category_rules[category]
        min_seed_time = rules.get('min_seed_time')
        min_ratio = rules.get('min_ratio')

        seed_time_met = min_seed_time is not None and torrent['seeding_time'] >= min_seed_time
        ratio_met = min_ratio is not None and torrent['ratio'] >= min_ratio

        if not (seed_time_met or ratio_met):
            continue

        # Grace period only applies when the torrent qualifies by ratio alone;
        # torrents that met their (long) min_seed_time have had plenty of
        # announces already.
        if ratio_met and not seed_time_met:
            grace = rules.get('ratio_grace', default_grace)
            if grace and grace > 0 and grace_state_path:
                first_seen = grace_state.get(torrent['hash'])
                if first_seen is None:
                    # First time we see this torrent at target ratio:
                    # start the clock, don't remove it yet.
                    grace_state[torrent['hash']] = now
                    grace_state_changed = True
                    held_back_count += 1
                    logger.debug(f"Ratio grace started for '{torrent['name'][:50]}...' "
                                 f"(ratio={torrent['ratio']:.2f}, grace={grace:.0f}s)")
                    continue
                if now - first_seen < grace:
                    held_back_count += 1
                    logger.debug(f"Ratio grace active for '{torrent['name'][:50]}...' "
                                 f"({now - first_seen:.0f}s of {grace:.0f}s elapsed)")
                    continue

        filtered_torrents.append(torrent)
        logger.debug(f"Torrent '{torrent['name'][:50]}...' eligible for removal: "
                     f"seed_time={torrent['seeding_time']:.0f}s (min={min_seed_time if min_seed_time else 'N/A'}), "
                     f"ratio={torrent['ratio']:.2f} (min={min_ratio if min_ratio else 'N/A'})")

    # Prune state entries for torrents that no longer exist in the client
    if grace_state_path:
        pruned_state = {h: t for h, t in grace_state.items() if h in current_hashes}
        if grace_state_changed or len(pruned_state) != len(grace_state):
            save_ratio_grace_state(grace_state_path, pruned_state, logger)

    if held_back_count > 0:
        logger.info(f"Ratio grace period: {held_back_count} torrent(s) held back from removal")

    return filtered_torrents


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

def _build_torrent_info(torrent: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields we keep about a removed torrent."""
    return {
        'hash': torrent['hash'],
        'name': torrent['name'],
        'size': torrent['size'],
        'seeding_time': torrent['seeding_time'],
        'ratio': torrent['ratio'],
        'category': torrent['category'],
    }


def _sort_candidates(candidates: List[Dict[str, Any]], ratio_log: Dict[str, List[Dict[str, Any]]],
                     logger: Logger, bonus_rules: Dict[str, Dict[str, Any]],
                     config: configparser.ConfigParser) -> List[Dict[str, Any]]:
    """Sort removal candidates: worst average weekly ratio first, ties broken by
    longest seed time, then largest size, then name for determinism."""
    for torrent in candidates:
        torrent['average_ratio'] = calculate_average_ratio(torrent, ratio_log, logger, bonus_rules, config)
    return sorted(candidates, key=lambda t: (t['average_ratio'], -t['seeding_time'], -t['size'], t['name']))


def remove_torrents_by_space(torrents: List[Dict[str, Any]], categories_space: List[str],
                             space_needed: float, logger: Logger, session: requests.Session,
                             api_address: str, test_mode: bool,
                             ratio_log: Dict[str, List[Dict[str, Any]]],
                             bonus_rules: Dict[str, Dict[str, Any]],
                             config: configparser.ConfigParser) -> List[Dict[str, Any]]:
    """Remove torrents (worst performers first) until enough space is freed.

    Hardlink checks are performed lazily, only on torrents actually about to
    be removed, and only successful removals count towards freed space.
    """
    torrents_removed_info: List[Dict[str, Any]] = []
    candidates = [t for t in torrents if t['category'].lower() in categories_space]

    if not candidates:
        return torrents_removed_info

    candidates = _sort_candidates(candidates, ratio_log, logger, bonus_rules, config)
    logger.info(f"Space check: {len(candidates)} candidate torrent(s), "
                f"need to free {space_needed:.1f} GB")

    space_freed = 0.0
    hardlinked_skipped = 0
    failed_removals = 0

    for torrent in candidates:
        if space_freed >= space_needed:
            break

        if has_hardlinked_files(torrent, session, api_address, logger, config):
            hardlinked_skipped += 1
            continue

        if not test_mode and not remove_torrent(session, api_address, torrent['hash'], True, logger):
            failed_removals += 1
            continue

        space_freed += torrent['size'] / BYTES_TO_GB
        torrents_removed_info.append(_build_torrent_info(torrent))

    if hardlinked_skipped:
        logger.info(f"Space check: skipped {hardlinked_skipped} hardlinked torrent(s)")
    if failed_removals:
        logger.warning(f"Space check: {failed_removals} removal(s) failed via API")
    if space_freed < space_needed:
        logger.warning(f"Space check: only freed {space_freed:.1f} GB of the "
                       f"{space_needed:.1f} GB needed (ran out of eligible torrents)")

    return torrents_removed_info


def remove_torrents_by_count(all_torrents: List[Dict[str, Any]], eligible_torrents: List[Dict[str, Any]],
                             categories_number: List[str], max_torrents: int, logger: Logger,
                             session: requests.Session, api_address: str, test_mode: bool,
                             ratio_log: Dict[str, List[Dict[str, Any]]],
                             bonus_rules: Dict[str, Dict[str, Any]], sort_by_size: bool,
                             config: configparser.ConfigParser) -> List[Dict[str, Any]]:
    """Keep each listed category at or below `max_torrents` torrents.

    The limit is measured against the TOTAL number of torrents in the
    category (from `all_torrents`), while removal candidates are only drawn
    from `eligible_torrents` (those that already satisfy the seed rules).
    If fewer torrents are eligible than the overshoot, the category will
    remain above the limit and a warning is logged.
    """
    torrents_removed_info: List[Dict[str, Any]] = []

    for category in categories_number:
        cat = category.lower()
        total_in_category = sum(1 for t in all_torrents if t.get('category', '').lower() == cat)
        num_to_remove = total_in_category - max_torrents

        if num_to_remove <= 0:
            logger.debug(f"Category '{category}': {total_in_category} torrents "
                         f"(within limit of {max_torrents})")
            continue

        candidates = [t for t in eligible_torrents if t.get('category', '').lower() == cat]
        logger.info(f"Category '{category}': {total_in_category} torrents exceeds limit of "
                    f"{max_torrents}; {len(candidates)} eligible for removal")

        if sort_by_size:
            candidates = sorted(candidates, key=lambda t: t['size'], reverse=True)
        else:
            candidates = _sort_candidates(candidates, ratio_log, logger, bonus_rules, config)

        removed_count = 0
        hardlinked_skipped = 0
        failed_removals = 0

        for torrent in candidates:
            if removed_count >= num_to_remove:
                break

            if has_hardlinked_files(torrent, session, api_address, logger, config):
                hardlinked_skipped += 1
                continue

            if not test_mode and not remove_torrent(session, api_address, torrent['hash'], True, logger):
                failed_removals += 1
                continue

            removed_count += 1
            torrents_removed_info.append(_build_torrent_info(torrent))

        logger.info(f"Category '{category}': removed {removed_count} of {num_to_remove} over limit")
        if hardlinked_skipped:
            logger.info(f"Category '{category}': skipped {hardlinked_skipped} hardlinked torrent(s)")
        if failed_removals:
            logger.warning(f"Category '{category}': {failed_removals} removal(s) failed via API")
        if removed_count < num_to_remove:
            logger.warning(f"Category '{category}': still {num_to_remove - removed_count} over the "
                           f"limit (not enough eligible torrents)")

    return torrents_removed_info
