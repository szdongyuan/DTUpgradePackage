from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import tempfile


class ProgramBundleError(Exception):
    pass


@dataclass(frozen=True)
class ProgramBundleResult:
    internal_replaced: bool


_EXPECTED_EXECUTABLE_LEAF = "DiTing.exe"
_EXPECTED_INTERNAL_LEAF = "_internal"
_COPY_BUFFER_SIZE = 1024 * 1024


def _resolved_parent_path(value):
    path = os.path.abspath(os.fspath(value))
    parent = os.path.realpath(os.path.dirname(path))
    return os.path.join(parent, os.path.basename(path))


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _validate_destination_paths(destination_executable, destination_internal):
    executable_parent = os.path.dirname(destination_executable)
    internal_parent = os.path.dirname(destination_internal)
    if not _same_path(executable_parent, internal_parent):
        raise ProgramBundleError("destination paths must have the same resolved parent")

    executable_leaf = os.path.basename(destination_executable)
    internal_leaf = os.path.basename(destination_internal)
    if os.path.normcase(executable_leaf) != os.path.normcase(_EXPECTED_EXECUTABLE_LEAF):
        raise ProgramBundleError(f"invalid destination leaf: {executable_leaf}")
    if os.path.normcase(internal_leaf) != os.path.normcase(_EXPECTED_INTERNAL_LEAF):
        raise ProgramBundleError(f"invalid destination leaf: {internal_leaf}")
    if os.path.normcase(executable_leaf) == os.path.normcase(internal_leaf):
        raise ProgramBundleError("destination leaves must be distinct")


def _is_junction(path):
    checker = getattr(Path(path), "is_junction", None)
    try:
        return bool(checker and checker())
    except OSError as error:
        raise ProgramBundleError(f"cannot inspect path for junction: {path}: {error}") from error


def _is_link_or_junction(path):
    return os.path.islink(path) or _is_junction(path)


def _stat_no_follow(path):
    return os.stat(path, follow_symlinks=False)


def _validate_regular_file(path, description, expected_stat=None):
    if not os.path.lexists(path):
        raise ProgramBundleError(f"{description} is missing: {path}")
    if _is_link_or_junction(path):
        raise ProgramBundleError(f"{description} must be a real regular file: {path}")
    try:
        current_stat = _stat_no_follow(path)
    except OSError as error:
        raise ProgramBundleError(f"cannot inspect {description}: {path}: {error}") from error
    if not stat.S_ISREG(current_stat.st_mode):
        raise ProgramBundleError(f"{description} must be a regular file: {path}")
    if expected_stat is not None and not _same_identity(current_stat, expected_stat):
        raise ProgramBundleError(f"{description} changed during staging: {path}")
    return current_stat


def _classify_internal_source(path):
    if not os.path.lexists(path):
        return None
    return _validate_real_directory(path, "source internal directory")


def _validate_real_directory(path, description, expected_stat=None):
    if not os.path.lexists(path):
        raise ProgramBundleError(f"{description} is missing: {path}")
    if _is_link_or_junction(path):
        raise ProgramBundleError(f"{description} must be a real directory: {path}")
    try:
        current_stat = _stat_no_follow(path)
    except OSError as error:
        raise ProgramBundleError(f"cannot inspect {description}: {path}: {error}") from error
    if not stat.S_ISDIR(current_stat.st_mode):
        raise ProgramBundleError(f"{description} must be a real directory: {path}")
    if expected_stat is not None and not _same_identity(current_stat, expected_stat):
        raise ProgramBundleError(f"{description} changed during staging: {path}")
    return current_stat


def _same_identity(left, right):
    return (
        stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _open_source_descriptor(path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _apply_file_metadata(destination, source_stat):
    try:
        os.chmod(destination, stat.S_IMODE(source_stat.st_mode), follow_symlinks=False)
    except Exception:
        pass
    try:
        os.utime(
            destination,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
    except Exception:
        pass


def _copy_regular_file(source, destination, expected_stat=None):
    source_stat = _validate_regular_file(source, "source file", expected_stat=expected_stat)
    try:
        descriptor = _open_source_descriptor(source)
    except OSError as error:
        raise ProgramBundleError(f"cannot open source file: {source}: {error}") from error

    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = _validate_regular_file(source, "source file", expected_stat=source_stat)
        if not _same_identity(descriptor_stat, path_stat):
            raise ProgramBundleError(f"source file changed while being opened: {source}")
        with os.fdopen(descriptor, "rb", closefd=True) as source_stream:
            descriptor = None
            with open(destination, "wb") as destination_stream:
                shutil.copyfileobj(source_stream, destination_stream, length=_COPY_BUFFER_SIZE)
    except ProgramBundleError:
        raise
    except OSError as error:
        raise ProgramBundleError(f"cannot copy source file: {source}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    _apply_file_metadata(destination, descriptor_stat)


def _apply_directory_metadata(destination, source_stat):
    _apply_file_metadata(destination, source_stat)


def _copy_directory_contents(source, destination, expected_stat):
    source_stat = _validate_real_directory(
        source,
        "source directory",
        expected_stat=expected_stat,
    )
    try:
        entries = os.scandir(source)
    except OSError as error:
        raise ProgramBundleError(f"cannot enumerate source directory: {source}: {error}") from error

    try:
        with entries:
            for entry in entries:
                source_entry = entry.path
                destination_entry = os.path.join(destination, entry.name)
                try:
                    entry_is_link = entry.is_symlink()
                    entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ProgramBundleError(f"cannot inspect source entry: {source_entry}: {error}") from error
                if entry_is_link or _is_link_or_junction(source_entry):
                    raise ProgramBundleError(f"source links and junctions are not allowed: {source_entry}")
                try:
                    entry_stat = _stat_no_follow(source_entry)
                except OSError as error:
                    raise ProgramBundleError(f"cannot inspect source entry: {source_entry}: {error}") from error

                if stat.S_ISREG(entry_stat.st_mode):
                    _copy_regular_file(source_entry, destination_entry, expected_stat=entry_stat)
                elif stat.S_ISDIR(entry_stat.st_mode):
                    os.mkdir(destination_entry)
                    _copy_directory_contents(source_entry, destination_entry, expected_stat=entry_stat)
                else:
                    raise ProgramBundleError(f"unsupported source entry: {source_entry}")
    except ProgramBundleError:
        raise
    except OSError as error:
        raise ProgramBundleError(f"cannot stage source directory: {source}: {error}") from error

    _apply_directory_metadata(destination, source_stat)


def _stage_internal_directory(source, destination, expected_stat):
    _copy_directory_contents(source, destination, expected_stat=expected_stat)


def _remove_leaf(path):
    if not path or not os.path.lexists(path):
        return
    if os.path.islink(path):
        os.unlink(path)
        return
    if _is_junction(path):
        os.rmdir(path)
        return
    object_stat = _stat_no_follow(path)
    if stat.S_ISDIR(object_stat.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _temporary_recovery_path(parent, leaf):
    return tempfile.mktemp(prefix=f".{leaf}.program-bundle-recovery-", dir=parent)


def _move_destination_to_recovery(destination):
    if not os.path.lexists(destination):
        return None
    recovery = _temporary_recovery_path(os.path.dirname(destination), os.path.basename(destination))
    os.replace(destination, recovery)
    return recovery


def _report_warning_best_effort(warning_reporter, message):
    if warning_reporter is None:
        return
    try:
        warning_reporter(message)
    except BaseException:
        pass


def _cleanup_committed_recovery(path, warning_reporter):
    if path is None:
        return
    try:
        _remove_leaf(path)
    except Exception as error:
        _report_warning_best_effort(
            warning_reporter,
            f"failed to clean retained recovery artifact {path}: {error}",
        )


def _cleanup_staging_best_effort(path):
    if path is None:
        return
    try:
        _remove_leaf(path)
    except Exception:
        pass


def replace_program_bundle(
    source_executable,
    destination_executable,
    source_internal,
    destination_internal,
    warning_reporter=None,
):
    source_executable = _resolved_parent_path(source_executable)
    destination_executable = _resolved_parent_path(destination_executable)
    source_internal = _resolved_parent_path(source_internal)
    destination_internal = _resolved_parent_path(destination_internal)
    _validate_destination_paths(destination_executable, destination_internal)

    executable_stat = _validate_regular_file(source_executable, "source executable")
    internal_stat = _classify_internal_source(source_internal)
    internal_participates = internal_stat is not None
    destination_parent = os.path.dirname(destination_executable)

    staged_executable = None
    staged_internal = None
    executable_recovery = None
    internal_recovery = None
    committed = False
    try:
        executable_descriptor, staged_executable = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination_executable)}.program-bundle-stage-",
            dir=destination_parent,
        )
        os.close(executable_descriptor)
        _copy_regular_file(source_executable, staged_executable, expected_stat=executable_stat)

        if internal_participates:
            staged_internal = tempfile.mkdtemp(
                prefix=f".{os.path.basename(destination_internal)}.program-bundle-stage-",
                dir=destination_parent,
            )
            _stage_internal_directory(source_internal, staged_internal, expected_stat=internal_stat)

        if internal_participates:
            internal_recovery = _move_destination_to_recovery(destination_internal)
        executable_recovery = _move_destination_to_recovery(destination_executable)

        if internal_participates:
            os.replace(staged_internal, destination_internal)
            staged_internal = None
        os.replace(staged_executable, destination_executable)
        staged_executable = None
        committed = True
    except ProgramBundleError:
        raise
    except Exception as error:
        raise ProgramBundleError(f"program bundle replacement failed: {error}") from error
    finally:
        _cleanup_staging_best_effort(staged_executable)
        _cleanup_staging_best_effort(staged_internal)

    if committed:
        _cleanup_committed_recovery(executable_recovery, warning_reporter)
        _cleanup_committed_recovery(internal_recovery, warning_reporter)

    return ProgramBundleResult(internal_replaced=internal_participates)
