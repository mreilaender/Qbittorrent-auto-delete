"""Logging setup for the qBittorrent cleanup scripts.

Writes everything to stdout with a tag-based prefix ([cleanup] or [scheduler])
so that multiple modules sharing the same process can be distinguished.
Log level is read from config.ini [logging] section, defaulting to INFO.
"""

import logging
import sys
import configparser
from typing import Optional


BYTES_TO_GB = 1024 ** 3
SECONDS_PER_WEEK = 7 * 86400
MAX_NAME_LENGTH = 69


def setup_logger(name: str = 'torrent_cleanup',
                 config: Optional[configparser.ConfigParser] = None) -> logging.Logger:
    """Create the application logger.

    Uses a dedicated named logger (not the root logger) so that DEBUG level
    doesn't pull in internal chatter from third-party libraries such as
    requests/urllib3, and clears existing handlers so repeated calls don't
    duplicate output.
    """
    formatter = logging.Formatter(
        '%(asctime)s [%(name)s] %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.propagate = False
    for existing_handler in list(logger.handlers):
        logger.removeHandler(existing_handler)
    logger.addHandler(handler)

    if config and config.has_option('logging', 'log_level'):
        log_level_str = config.get('logging', 'log_level').upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
    else:
        log_level = logging.INFO

    logger.setLevel(log_level)
    return logger


def log_torrent_removal_info(torrents_info: list, logger: logging.Logger,
                             bonus_rules: dict,
                             config: configparser.ConfigParser,
                             ratio_log: dict) -> None:
    """Log one formatted line per removed (or would-be removed) torrent."""
    if not torrents_info:
        logger.info("No torrents to remove based on current rules.")
        return

    logger.info(f"Total torrents to remove: {len(torrents_info)}")
    for torrent_info in torrents_info:
        size_gb = torrent_info['size'] / BYTES_TO_GB
        seeding_time_week = torrent_info['seeding_time'] / SECONDS_PER_WEEK
        category = torrent_info.get('category', 'Unknown')
        average_ratio_per_week = _calculate_average_ratio(
            torrent_info, ratio_log, logger, bonus_rules, config)
        truncated_name = (torrent_info['name'][:MAX_NAME_LENGTH - 3] + '...') \
            if len(torrent_info['name']) > MAX_NAME_LENGTH else torrent_info['name']
        size_str = f"{size_gb:.2f} GB".rjust(10)
        seeding_time_str = f"{seeding_time_week:.1f} Weeks".rjust(11)
        ratio_week_str = f"{average_ratio_per_week:.3f} R/W".rjust(11)
        logger.info(f"{truncated_name:<69}\t{category}\t{size_str}\t{seeding_time_str}\t{ratio_week_str}")


def _calculate_average_ratio(torrent: dict, ratio_log: dict, logger: logging.Logger,
                             bonus_rules: dict, config: configparser.ConfigParser) -> float:
    """Calculate the bonus-adjusted average weekly ratio change for a torrent.

    Mirror of torrent_utils.calculate_average_ratio to avoid a circular import
    between main.py (which calls log_torrent_removal_info) and torrent_utils
    (which is already imported).
    """
    import torrent_utils

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

    average_ratio_change *= torrent_utils.apply_bonus_rules(torrent, bonus_rules, logger)

    return average_ratio_change
