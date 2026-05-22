import argparse
import ctypes
import sys

from base.db_upgrade import STATUS_ALREADY_UPGRADED, STATUS_SUCCESS, upgrade_legacy_single_database


def _show_message(title, message, is_error=False):
    if sys.platform.startswith("win"):
        style = 0x10 if is_error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, title, style)
    else:
        print(f"{title}: {message}")


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Upgrade a legacy single SQLite database into split audio/system databases."
    )
    parser.add_argument(
        "--database-dir",
        dest="database_dir",
        help="Directory containing the legacy audio_data.db file.",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    status, message = upgrade_legacy_single_database(database_dir=args.database_dir)
    is_error = status not in {STATUS_SUCCESS, STATUS_ALREADY_UPGRADED}
    title = "DB Upgrade Failed" if is_error else "DB Upgrade"
    _show_message(title, message, is_error=is_error)
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.exit(main())
