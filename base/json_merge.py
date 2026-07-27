import json
import os
import tempfile


def merge_json_files(first_json_path, second_json_path):
    return merge_json_files_with_reason(first_json_path, second_json_path)[0]


def merge_json_files_with_reason(first_json_path, second_json_path):
    first_path = _lexical_absolute_path(first_json_path)
    second_path = _lexical_absolute_path(second_json_path)
    if first_path is None or second_path is None:
        return False, "invalid_path"

    try:
        if not os.path.isfile(first_path) or not os.path.isfile(second_path):
            return False, "missing_file"
        if os.path.samefile(first_path, second_path):
            return False, "same_file"
    except OSError:
        return False, "unreadable_file"

    try:
        with open(first_path, "r", encoding="utf-8-sig") as first_file:
            first_data = json.load(first_file)
        with open(second_path, "r", encoding="utf-8-sig") as second_file:
            second_data = json.load(second_file)
    except (OSError, UnicodeError):
        return False, "unreadable_file"
    except (ValueError, RecursionError):
        return False, "invalid_json"

    if not isinstance(first_data, dict) or not isinstance(second_data, dict):
        return False, "non_object_root"

    merged = dict(second_data)
    merged.update(first_data)
    return _replace_with_json(first_path, merged)


def _lexical_absolute_path(path):
    try:
        filesystem_path = os.fspath(path)
    except (OSError, TypeError, ValueError):
        return None
    if (
        not isinstance(filesystem_path, str)
        or not filesystem_path
        or "\x00" in filesystem_path
    ):
        return None
    try:
        return os.path.abspath(filesystem_path)
    except (OSError, ValueError):
        return None


def _replace_with_json(target_path, data):
    file_descriptor = None
    temporary_path = None
    committed = False
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=os.path.dirname(target_path),
        )
        temporary_file = os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        )
        file_descriptor = None
        with temporary_file:
            json.dump(
                data,
                temporary_file,
                indent=6,
                ensure_ascii=False,
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target_path)
        committed = True
        return True, "ok"
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError):
        return False, "write_failed"
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None and not committed:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
