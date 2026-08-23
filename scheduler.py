"""Schedule-based supervisor for qBittorrent cleanup.

Runs the cleanup job at a configurable interval and optionally logs torrent
ratios daily.  Both jobs share the same config.ini and state directory.

CLI entry points:
    python scheduler.py                       # direct execution
    qbittorrent-scheduler                     # via pip install
"""

import sys
import os
import time
import signal
import argparse
import configparser
from logging import Logger

import schedule

import logger_utils
import torrent_utils

__version__ = "0.1.0"

# Globals for graceful shutdown
_shutdown_requested = False
_scheduler_logger: Logger = None


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def _resolve_config_path() -> str:
    """Resolve config path: CONFIG_PATH env → --config flag → STATE_DIR/config.ini → ./config.ini."""
    from_env = os.environ.get('CONFIG_PATH', '').strip()
    if from_env:
        return from_env
    return './config.ini'


def _resolve_state_dir() -> str:
    """Resolve state directory: STATE_DIR env → script directory."""
    from_env = os.environ.get('STATE_DIR', '').strip()
    if from_env:
        return from_env
    return os.path.dirname(os.path.abspath(__file__))


def _load_config(config_path: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def run_cleanup_job(config: configparser.ConfigParser, state_dir: str) -> None:
    """Execute the cleanup job as a scheduled task."""
    global _shutdown_requested
    logger = logger_utils.setup_logger(name='torrent_cleanup', config=config)
    logger.info("Running cleanup job")

    session = requests.Session()
    try:
        torrent_utils.setup_session_auth(session, config)
        exit_code = main.run_cleanup(config, logger, session, test_mode=False, state_dir=state_dir)
        if exit_code != 0:
            logger.warning("Cleanup job finished with exit code %d", exit_code)
    except Exception as e:
        logger.error("Cleanup job failed: %s", e)
    finally:
        session.close()


def run_ratio_log_job(config: configparser.ConfigParser, state_dir: str) -> None:
    """Execute the ratio logger job (if the module is available)."""
    global _shutdown_requested
    logger = logger_utils.setup_logger(name='torrent_cleanup', config=config)
    logger.info("Running ratio log job")

    try:
        import torrent_ratio_logger
    except ImportError:
        logger.info("torrent_ratio_logger module not available, skipping ratio log job")
        return

    session = requests.Session()
    try:
        torrent_utils.setup_session_auth(session, config)
        api_address = config.get('login', 'address')
        log_file_path = os.path.join(state_dir, 'torrent_ratio_log.json')
        max_entries = config.getint('torrent_ratio_logger', 'max_entries', fallback=28)
        purge_days_str = config.get('torrent_ratio_logger', 'purge_days', fallback='')
        purge_days = [int(day.strip()) for day in purge_days_str.split(',') if day.strip()]

        try:
            success = torrent_ratio_logger.update_ratio_log(
                config, log_file_path, logger, max_entries, purge_days)
        except Exception as e:
            logger.error("Ratio log job failed: %s", e)
            success = False
    finally:
        session.close()


def main() -> int:
    """Scheduler entry point.

    Runs the cleanup job at a configurable interval and an optional ratio
    log job once per day.  Supports graceful shutdown via SIGTERM/SIGINT.
    """
    import requests  # lazy, only needed here

    global _shutdown_requested

    parser = argparse.ArgumentParser(description='qBittorrent cleanup scheduler')
    parser.add_argument('--config', default=None, help='Path to config.ini')
    parser.add_argument('--cleanup-interval', type=int, default=None,
                        help='Cleanup interval in hours (default: CLEANUP_INTERVAL env, then 1)')
    parser.add_argument('--no-cleanup', action='store_true',
                        help='Disable the cleanup job (only run ratio log if available)')
    args = parser.parse_args()

    # Resolve config path
    if args.config:
        config_path = args.config
    else:
        config_path = _resolve_config_path()

    # Resolve state directory
    state_dir = _resolve_state_dir()

    # Load config
    config = _load_config(config_path)

    # Setup logger
    global _scheduler_logger
    _scheduler_logger = logger_utils.setup_logger(name='scheduler', config=config)

    # Resolve cleanup interval
    interval = args.cleanup_interval
    if interval is None:
        interval = int(os.environ.get('CLEANUP_INTERVAL', '1'))

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Register jobs
    if not args.no_cleanup:
        schedule.every(interval).hours.do(run_cleanup_job, config, state_dir)
        _scheduler_logger.info("Cleanup job scheduled every %d hour(s)", interval)
    else:
        _scheduler_logger.info("Cleanup job disabled (--no-cleanup)")

    # Ratio log runs once per day at midnight (if module is available)
    if not args.no_cleanup:
        schedule.every().day.at("00:00").do(run_ratio_log_job, config, state_dir)
        _scheduler_logger.info("Ratio log job scheduled daily at midnight")
    else:
        schedule.every().day.at("00:00").do(run_ratio_log_job, config, state_dir)
        _scheduler_logger.info("Ratio log job scheduled daily at midnight")

    _scheduler_logger.info("Scheduler started (state_dir=%s)", state_dir)

    # Main loop
    while not _shutdown_requested:
        schedule.run_pending()
        time.sleep(60)

    _scheduler_logger.info("Shutdown requested, draining...")
    # Drain remaining scheduled jobs
    while not _shutdown_requested:
        schedule.run_pending()
        time.sleep(5)

    _scheduler_logger.info("Scheduler exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
