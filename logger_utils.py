"""Logging setup for the qBittorrent cleanup scripts.

Uses a prepending log file (newest run at the top) with size-based rotation.
Entries are buffered in memory during the run and flushed once at the end via
write_log_entries(), so a crash mid-run should still flush in a finally block.
"""

import logging
import os
import configparser
from logging.handlers import RotatingFileHandler
from typing import Tuple, List, Dict, Any

import torrent_utils

# Constants
LOGGER_NAME = 'torrent_cleanup'
MAX_BYTES = 1 * 1024 * 1024  # 1 MB
BACKUP_COUNT = 3
SEPARATOR_LENGTH = 127
MAX_NAME_LENGTH = 69
BYTES_TO_GB = 1024 ** 3
SECONDS_PER_WEEK = 7 * 86400


class PrependingRotatingFileHandler(RotatingFileHandler):
    """Buffers log entries and prepends them to the log file so the newest
    run appears at the top. The first entry of a run gets the full formatted
    header (timestamp/level); subsequent entries are written as raw messages.

    Rotation is checked after flushing (when the real file size is known),
    which is more accurate than checking against the on-disk size while
    entries are still buffered in memory.
    """

    def __init__(self, *args, **kwargs):
        # delay=True: never open a write stream; we manage the file ourselves.
        kwargs['delay'] = True
        super().__init__(*args, **kwargs)
        self.log_entries: List[str] = []
        self.first_entry = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.first_entry:
                log_entry = "-" * SEPARATOR_LENGTH + "\n" + self.format(record)
                self.first_entry = False
            else:
                log_entry = record.getMessage()
                # Keep warnings/errors identifiable in the compact format
                if record.levelno >= logging.WARNING:
                    log_entry = f"{record.levelname}: {log_entry}"
            self.log_entries.append(log_entry)
        except Exception:
            self.handleError(record)

    def write_log_entries(self) -> None:
        """Flush buffered entries to the top of the log file, then rotate if
        the file has grown past maxBytes."""
        if not self.log_entries:
            return
        try:
            existing_content = ''
            if os.path.exists(self.baseFilename):
                with open(self.baseFilename, 'r') as file:
                    existing_content = file.read()
            with open(self.baseFilename, 'w') as file:
                file.write('\n'.join(self.log_entries) + '\n' + existing_content)

            if self.maxBytes > 0 and os.path.getsize(self.baseFilename) >= self.maxBytes:
                self.doRollover()
        except (IOError, OSError) as e:
            print(f"Error writing log entries: {e}")
        finally:
            self.log_entries = []
            self.first_entry = True


def setup_logger(log_file_name: str = 'deletelog.txt',
                 config: configparser.ConfigParser = None) -> Tuple[logging.Logger, PrependingRotatingFileHandler]:
    """Create the application logger.

    Uses a dedicated named logger (not the root logger) so that DEBUG level
    doesn't pull in internal chatter from third-party libraries such as
    requests/urllib3, and clears existing handlers so repeated calls don't
    duplicate output.
    """
    script_directory = os.path.dirname(os.path.abspath(__file__))
    log_file_path = os.path.join(script_directory, log_file_name)

    handler = PrependingRotatingFileHandler(log_file_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(log_formatter)

    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    for existing_handler in list(logger.handlers):
        logger.removeHandler(existing_handler)
    logger.addHandler(handler)

    # Get log level from config, default to INFO
    if config and config.has_option('logging', 'log_level'):
        log_level_str = config.get('logging', 'log_level').upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
    else:
        log_level = logging.INFO

    logger.setLevel(log_level)
    return logger, handler


def log_torrent_removal_info(torrents_info: List[Dict[str, Any]], logger: logging.Logger,
                             bonus_rules: Dict[str, Dict[str, Any]],
                             config: configparser.ConfigParser,
                             ratio_log: Dict[str, List[Dict[str, Any]]]) -> None:
    """Log one formatted line per removed (or would-be removed) torrent."""
    if not torrents_info:
        logger.info("No torrents to remove based on current rules.")
        return

    logger.info(f"Total torrents to remove: {len(torrents_info)}")
    for torrent_info in torrents_info:
        size_gb = torrent_info['size'] / BYTES_TO_GB
        seeding_time_week = torrent_info['seeding_time'] / SECONDS_PER_WEEK
        category = torrent_info.get('category', 'Unknown')
        average_ratio_per_week = torrent_utils.calculate_average_ratio(
            torrent_info, ratio_log, logger, bonus_rules, config)
        truncated_name = (torrent_info['name'][:MAX_NAME_LENGTH - 3] + '...') \
            if len(torrent_info['name']) > MAX_NAME_LENGTH else torrent_info['name']
        size_str = f"{size_gb:.2f} GB".rjust(10)
        seeding_time_str = f"{seeding_time_week:.1f} Weeks".rjust(11)
        ratio_week_str = f"{average_ratio_per_week:.3f} R/W".rjust(11)
        logger.info(f"{truncated_name:<69}  \t{category} \t{size_str} \t{seeding_time_str} \t{ratio_week_str}")
