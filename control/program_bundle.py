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


def _prepare_destination_recovery(destination):
    if not os.path.lexists(destination):
        return None
    return _temporary_recovery_path(os.path.dirname(destination), os.path.basename(destination))


def _report_warning_best_effort(warning_reporter, message):
    if warning_reporter is None:
        return
    try:
        warning_reporter(message)
    except Exception:
        pass


def _remember_control_flow(first_control_flow, error):
    if first_control_flow is None and not isinstance(error, Exception):
        return error
    return first_control_flow


def _artifact_exists(path):
    return path is not None and os.path.lexists(path)


def _probe_artifact(path, description, problems, first_control_flow):
    if path is None:
        return False, first_control_flow
    try:
        return _artifact_exists(path), first_control_flow
    except BaseException as error:
        first_control_flow = _remember_control_flow(first_control_flow, error)
        problems.append(f"failed to inspect {description} {path}: {error}")
        return None, first_control_flow


def _format_artifacts(kind, paths):
    if not paths:
        return None
    noun = "artifact" if len(paths) == 1 else "artifacts"
    return f"retained {kind} {noun}: {', '.join(paths)}"


def _report_messages(warning_reporter, messages, first_control_flow):
    for message in messages:
        try:
            _report_warning_best_effort(warning_reporter, message)
        except BaseException as error:
            first_control_flow = _remember_control_flow(first_control_flow, error)
    return first_control_flow


def _remove_artifact(path, description, problems, first_control_flow):
    exists, first_control_flow = _probe_artifact(
        path,
        description,
        problems,
        first_control_flow,
    )
    if exists is not True:
        return first_control_flow
    try:
        _remove_leaf(path)
    except BaseException as error:
        first_control_flow = _remember_control_flow(first_control_flow, error)
        retained, first_control_flow = _probe_artifact(
            path,
            description,
            problems,
            first_control_flow,
        )
        if retained is True:
            problems.append(f"failed to remove {description} {path}: {error}")
    return first_control_flow


def _restore_recovery(recovery, destination, description, problems, first_control_flow):
    recovery_exists, first_control_flow = _probe_artifact(
        recovery,
        f"{description} recovery artifact",
        problems,
        first_control_flow,
    )
    if recovery_exists is not True:
        return first_control_flow
    destination_exists, first_control_flow = _probe_artifact(
        destination,
        f"{description} destination",
        problems,
        first_control_flow,
    )
    if destination_exists is None:
        return first_control_flow
    if destination_exists:
        problems.append(
            f"cannot restore {description}; destination remains occupied and recovery is retained: "
            f"{recovery}"
        )
        return first_control_flow
    try:
        os.replace(recovery, destination)
    except BaseException as error:
        first_control_flow = _remember_control_flow(first_control_flow, error)
        retained, first_control_flow = _probe_artifact(
            recovery,
            f"{description} recovery artifact",
            problems,
            first_control_flow,
        )
        if retained is True:
            problems.append(f"failed to restore {description} from {recovery}: {error}")
    return first_control_flow


def _collect_retained_artifacts(paths, kind, problems, first_control_flow):
    retained = []
    for path in paths:
        exists, first_control_flow = _probe_artifact(
            path,
            f"{kind} artifact",
            problems,
            first_control_flow,
        )
        if exists is True:
            retained.append(path)
    return retained, first_control_flow


def _probe_expected_identity(path, expected_stat, description, problems, first_control_flow):
    exists, first_control_flow = _probe_artifact(
        path,
        description,
        problems,
        first_control_flow,
    )
    if exists is not True:
        return False if exists is False else None, first_control_flow
    try:
        current_stat = _stat_no_follow(path)
    except BaseException as error:
        first_control_flow = _remember_control_flow(first_control_flow, error)
        problems.append(f"failed to inspect identity of {description} {path}: {error}")
        return None, first_control_flow
    return _same_identity(current_stat, expected_stat), first_control_flow


def _reconcile_interrupted_installations(state, problems, first_control_flow):
    for name, description in (
        ("executable", "staged executable"),
        ("internal", "staged internal directory"),
    ):
        if state[f"{name}_installed"] or not state[f"{name}_install_started"]:
            continue

        staged_exists, first_control_flow = _probe_artifact(
            state[f"staged_{name}"],
            description,
            problems,
            first_control_flow,
        )
        destination_matches, first_control_flow = _probe_expected_identity(
            state[f"destination_{name}"],
            state[f"staged_{name}_identity"],
            f"installed {name}",
            problems,
            first_control_flow,
        )
        if destination_matches is True:
            state[f"{name}_installed"] = True
        elif staged_exists is False and destination_matches is False:
            problems.append(
                f"cannot reconcile interrupted {name} installation; staged and destination "
                "objects do not contain the staged identity"
            )
    return first_control_flow


def _rollback_transaction(state, primary, warning_reporter):
    first_control_flow = _remember_control_flow(None, primary)
    problems = []

    first_control_flow = _reconcile_interrupted_installations(
        state,
        problems,
        first_control_flow,
    )

    if state["executable_installed"]:
        first_control_flow = _remove_artifact(
            state["destination_executable"],
            "newly installed executable",
            problems,
            first_control_flow,
        )
    if state["internal_installed"]:
        first_control_flow = _remove_artifact(
            state["destination_internal"],
            "newly installed internal directory",
            problems,
            first_control_flow,
        )

    if state["executable_displacement_started"]:
        first_control_flow = _restore_recovery(
            state["executable_recovery"],
            state["destination_executable"],
            "prior executable",
            problems,
            first_control_flow,
        )
    if state["internal_displacement_started"]:
        first_control_flow = _restore_recovery(
            state["internal_recovery"],
            state["destination_internal"],
            "prior internal object",
            problems,
            first_control_flow,
        )

    first_control_flow = _remove_artifact(
        state["staged_executable"],
        "staging artifact",
        problems,
        first_control_flow,
    )
    first_control_flow = _remove_artifact(
        state["staged_internal"],
        "staging artifact",
        problems,
        first_control_flow,
    )

    retained_staging, first_control_flow = _collect_retained_artifacts(
        (state["staged_executable"], state["staged_internal"]),
        "staging",
        problems,
        first_control_flow,
    )
    retained_recovery, first_control_flow = _collect_retained_artifacts(
        (state["executable_recovery"], state["internal_recovery"]),
        "recovery",
        problems,
        first_control_flow,
    )
    artifact_messages = [
        message
        for message in (
            _format_artifacts("staging", retained_staging),
            _format_artifacts("recovery", retained_recovery),
        )
        if message is not None
    ]
    first_control_flow = _report_messages(
        warning_reporter,
        problems + artifact_messages,
        first_control_flow,
    )

    if first_control_flow is not None:
        raise first_control_flow

    detail = str(primary)
    if not isinstance(primary, ProgramBundleError):
        detail = f"program bundle replacement failed: {detail}"
    additions = problems + artifact_messages
    if additions:
        detail = f"{detail}; {'; '.join(additions)}"
    raise ProgramBundleError(detail) from primary


def _cleanup_after_commit(recovery_paths, warning_reporter):
    first_control_flow = None
    messages = []
    for recovery in recovery_paths:
        exists, first_control_flow = _probe_artifact(
            recovery,
            "recovery artifact",
            messages,
            first_control_flow,
        )
        if exists is not True:
            continue
        try:
            _remove_leaf(recovery)
        except BaseException as error:
            first_control_flow = _remember_control_flow(first_control_flow, error)
            retained, first_control_flow = _probe_artifact(
                recovery,
                "recovery artifact",
                messages,
                first_control_flow,
            )
            if retained is True:
                messages.append(f"failed to clean retained recovery artifact {recovery}: {error}")

    first_control_flow = _report_messages(
        warning_reporter,
        messages,
        first_control_flow,
    )
    if first_control_flow is not None:
        raise first_control_flow


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

    state = {
        "destination_executable": destination_executable,
        "destination_internal": destination_internal,
        "staged_executable": None,
        "staged_internal": None,
        "executable_recovery": None,
        "internal_recovery": None,
        "executable_displacement_started": False,
        "internal_displacement_started": False,
        "executable_install_started": False,
        "internal_install_started": False,
        "staged_executable_identity": None,
        "staged_internal_identity": None,
        "executable_installed": False,
        "internal_installed": False,
    }
    try:
        executable_descriptor, state["staged_executable"] = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination_executable)}.program-bundle-stage-",
            dir=destination_parent,
        )
        os.close(executable_descriptor)
        _copy_regular_file(
            source_executable,
            state["staged_executable"],
            expected_stat=executable_stat,
        )
        state["staged_executable_identity"] = _stat_no_follow(state["staged_executable"])

        if internal_participates:
            state["staged_internal"] = tempfile.mkdtemp(
                prefix=f".{os.path.basename(destination_internal)}.program-bundle-stage-",
                dir=destination_parent,
            )
            _stage_internal_directory(
                source_internal,
                state["staged_internal"],
                expected_stat=internal_stat,
            )
            state["staged_internal_identity"] = _stat_no_follow(state["staged_internal"])

        if internal_participates:
            state["internal_recovery"] = _prepare_destination_recovery(destination_internal)
            if state["internal_recovery"] is not None:
                state["internal_displacement_started"] = True
                os.replace(destination_internal, state["internal_recovery"])
        state["executable_recovery"] = _prepare_destination_recovery(destination_executable)
        if state["executable_recovery"] is not None:
            state["executable_displacement_started"] = True
            os.replace(destination_executable, state["executable_recovery"])

        if internal_participates:
            state["internal_install_started"] = True
            os.replace(state["staged_internal"], destination_internal)
            state["staged_internal"] = None
            state["internal_installed"] = True
        state["executable_install_started"] = True
        os.replace(state["staged_executable"], destination_executable)
        state["staged_executable"] = None
        state["executable_installed"] = True
    except BaseException as error:
        _rollback_transaction(state, error, warning_reporter)

    _cleanup_after_commit(
        (state["executable_recovery"], state["internal_recovery"]),
        warning_reporter,
    )

    return ProgramBundleResult(internal_replaced=internal_participates)
