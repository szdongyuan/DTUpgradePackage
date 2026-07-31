import hashlib
import ntpath
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

from base.json_merge import merge_json_files_with_reason

from consts.program_config import BACKUP_DIR, DEFAULT_DIR, TARGET_VERSION, ALL_VERSIONS, BACKUP_UI_PIC_PATH, CONFIG_DIR
from consts.program_config import AI_MODEL_CONFIG_PATH, SCANNER_BARCODE_CONFIG_PATH, UI_CONFIG_PATH, UI_PIC_PATH
from consts.program_config import BACKUP_AI_MODEL_CONFIG_PATH, BACKUP_SCANNER_BARCODE_CONFIG_PATH, BACKUP_UI_CONFIG_PATH
from consts.program_config import DEFAULT_TARGET_DIR

from load_yml.load_yml import Readyml


class ConfigManager(object):

    def __init__(self, logger=None, current_version=None):
        self.logger = logger
        self.current_version = current_version

        self.ui_pic_flag = True
        self.ui_config_flag = True
        self.ai_model_config_flag = True
        self.scannre_barcode_config_flag = True
        self.backup_file_path = list()
        self.upgrade_version_list = list()

        self.init_upgrade_version_list()

    def _report_best_effort(
        self,
        level,
        message,
        include_print=True,
    ):
        if include_print:
            try:
                print(message)
            except BaseException:
                pass
        try:
            getattr(self.logger, level)(message)
        except BaseException:
            pass

    def is_parent_path_os(self, parent_path, child_path):
        """
        使用os.path判断parent_path是否为child_path的父路径
        """
        try:
            common = os.path.commonpath([parent_path, child_path])
            return os.path.normpath(common) == os.path.normpath(parent_path)
        except (ValueError, OSError):
            return False

    def smart_copy(self, src_path, dst_path):
        """
        智能复制方法，自动处理文件和目录

        Args:
            src_path (str): 源路径
            dst_path (str): 目标路径
        """
        try:
            if not os.path.exists(src_path):
                self._report_best_effort(
                    "error",
                    f"源路径不存在: {src_path}",
                    include_print=False,
                )
                return False

            # 处理源路径是目录的情况
            if os.path.isdir(src_path):
                # 如果目标路径末尾有路径分隔符或目标不存在，则复制整个目录
                if os.path.exists(dst_path) and os.path.isdir(dst_path):
                    # 目标是目录，将源目录内容复制到目标目录
                    for item in os.listdir(src_path):
                        src_item = os.path.join(src_path, item)
                        dst_item = os.path.join(dst_path, item)
                        if os.path.isfile(src_item):
                            shutil.copy2(src_item, dst_item)
                        else:
                            if os.path.exists(dst_item):
                                shutil.rmtree(dst_item)
                            shutil.copytree(src_item, dst_item)
                    self._report_best_effort(
                        "info",
                        f"目录内容复制成功: {src_path} -> {dst_path}",
                    )
                else:
                    # 目标是文件路径或不存在，复制整个目录
                    if os.path.exists(dst_path):
                        if os.path.isfile(dst_path):
                            os.remove(dst_path)
                        else:
                            shutil.rmtree(dst_path)
                    shutil.copytree(src_path, dst_path)
                    self._report_best_effort(
                        "info",
                        f"目录复制成功: {src_path} -> {dst_path}",
                    )

            # 处理源路径是文件的情况
            elif os.path.isfile(src_path):
                # 确保目标目录存在
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir)

                shutil.copy2(src_path, dst_path)
                self._report_best_effort(
                    "info",
                    f"文件复制成功: {src_path} -> {dst_path}",
                )

            return True

        except Exception as e:
            self._report_best_effort(
                "error",
                f"复制失败 {src_path} -> {dst_path}: {e}",
            )
            return False

    @staticmethod
    def _snapshot_entry_is_link_or_reparse(entry_stat):
        reparse_flag = getattr(
            stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        file_attributes = getattr(entry_stat, "st_file_attributes", 0)
        return stat.S_ISLNK(entry_stat.st_mode) or bool(
            file_attributes & reparse_flag
        )

    def _snapshot_ancestor_chain_is_safe(self, path, trusted_anchor):
        try:
            absolute_path = os.path.abspath(path)
            absolute_anchor = os.path.abspath(trusted_anchor)
            if not self._path_is_contained(
                absolute_path,
                absolute_anchor,
            ):
                raise ValueError("path is outside its trusted anchor")

            resolved_anchor = os.path.realpath(absolute_anchor)
            anchor_stat = os.stat(absolute_anchor)
            visited_identities = {
                (anchor_stat.st_dev, anchor_stat.st_ino)
            }
            relative_path = os.path.relpath(
                absolute_path,
                absolute_anchor,
            )
        except (OSError, TypeError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照祖先安全检查失败 {path}: {e}",
                include_print=False,
            )
            return False

        if relative_path == os.curdir:
            return True

        current_path = absolute_anchor
        for path_component in relative_path.split(os.sep):
            current_path = os.path.join(current_path, path_component)
            try:
                os.lstat(current_path)
            except FileNotFoundError:
                break
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照祖先安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False

            try:
                resolved_path = os.path.realpath(current_path)
                if not self._path_is_contained(
                    resolved_path,
                    resolved_anchor,
                ):
                    self._report_best_effort(
                        "error",
                        f"快照祖先逃逸可信目录: {current_path}",
                        include_print=False,
                    )
                    return False

                followed_stat = os.stat(current_path)
                identity = (
                    followed_stat.st_dev,
                    followed_stat.st_ino,
                )
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照祖先安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False

            if identity in visited_identities:
                self._report_best_effort(
                    "error",
                    f"快照祖先循环链接: {current_path}",
                    include_print=False,
                )
                return False
            visited_identities.add(identity)

        return True

    def _inspect_snapshot_path(self, root_path):
        try:
            absolute_root = os.path.abspath(root_path)
            resolved_root = os.path.realpath(absolute_root)
            root_stat = os.stat(absolute_root)
        except (OSError, TypeError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照安全检查失败 {root_path}: {e}",
                include_print=False,
            )
            return False, ()

        if not stat.S_ISDIR(root_stat.st_mode):
            return True, ()

        root_identity = (root_stat.st_dev, root_stat.st_ino)
        pending_directories = [
            (absolute_root, frozenset((root_identity,)))
        ]
        visited_directory_identities = {root_identity}
        aliases = []

        while pending_directories:
            current_path, ancestor_identities = (
                pending_directories.pop()
            )
            try:
                with os.scandir(current_path) as directory_entries:
                    entries = list(directory_entries)
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False, ()

            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except (OSError, ValueError) as e:
                    self._report_best_effort(
                        "error",
                        f"快照安全检查失败 {entry.path}: {e}",
                        include_print=False,
                    )
                    return False, ()

                if self._snapshot_entry_is_link_or_reparse(entry_stat):
                    is_junction = bool(
                        os.name == "nt"
                        and (
                            getattr(
                                os.path,
                                "isjunction",
                                lambda _path: False,
                            )(entry.path)
                            or getattr(entry_stat, "st_reparse_tag", None)
                            == getattr(
                                stat,
                                "IO_REPARSE_TAG_MOUNT_POINT",
                                0xA0000003,
                            )
                        )
                    )
                    if not is_junction and not entry.is_symlink():
                        self._report_best_effort(
                            "error",
                            f"快照拒绝不支持的重解析点: {entry.path}",
                            include_print=False,
                        )
                        return False, ()

                    try:
                        resolved_target = os.path.realpath(entry.path)
                        if not self._path_is_contained(
                            resolved_target,
                            resolved_root,
                        ):
                            self._report_best_effort(
                                "error",
                                f"快照拒绝外部链接或重解析点: {entry.path}",
                                include_print=False,
                            )
                            return False, ()

                        followed_stat = os.stat(entry.path)
                        target_identity = (
                            followed_stat.st_dev,
                            followed_stat.st_ino,
                        )
                    except (OSError, ValueError) as e:
                        self._report_best_effort(
                            "error",
                            f"快照链接安全检查失败 {entry.path}: {e}",
                            include_print=False,
                        )
                        return False, ()

                    target_is_directory = stat.S_ISDIR(
                        followed_stat.st_mode
                    )
                    if (
                        target_is_directory
                        and target_identity in ancestor_identities
                    ):
                        self._report_best_effort(
                            "error",
                            f"快照拒绝循环链接或重解析点: {entry.path}",
                            include_print=False,
                        )
                        return False, ()

                    try:
                        relative_entry = os.path.relpath(
                            entry.path,
                            absolute_root,
                        )
                        relative_target = os.path.relpath(
                            resolved_target,
                            resolved_root,
                        )
                        link_target = (
                            None
                            if is_junction
                            else os.readlink(entry.path)
                        )
                    except (OSError, ValueError) as e:
                        self._report_best_effort(
                            "error",
                            f"快照链接安全检查失败 {entry.path}: {e}",
                            include_print=False,
                        )
                        return False, ()

                    aliases.append(
                        (
                            relative_entry,
                            relative_target,
                            "junction" if is_junction else "symlink",
                            link_target,
                            target_is_directory,
                        )
                    )
                    continue

                if not stat.S_ISDIR(entry_stat.st_mode):
                    continue

                identity = (entry_stat.st_dev, entry_stat.st_ino)
                if identity in ancestor_identities:
                    self._report_best_effort(
                        "error",
                        f"快照拒绝目录循环: {entry.path}",
                        include_print=False,
                    )
                    return False, ()
                if identity in visited_directory_identities:
                    continue
                visited_directory_identities.add(identity)
                pending_directories.append(
                    (
                        entry.path,
                        ancestor_identities | frozenset((identity,)),
                    )
                )

        aliases.sort(key=lambda alias: alias[0])
        return True, tuple(aliases)

    def _snapshot_path_is_safe(self, root_path):
        path_is_safe, _aliases = self._inspect_snapshot_path(root_path)
        return path_is_safe

    def _recreate_snapshot_aliases(
        self,
        staging_path,
        destination_path,
        aliases,
    ):
        for (
            relative_entry,
            relative_target,
            alias_kind,
            link_target,
            target_is_directory,
        ) in sorted(
            aliases,
            key=lambda alias: alias[0].count(os.sep),
            reverse=True,
        ):
            staged_alias = os.path.join(staging_path, relative_entry)
            try:
                if os.path.isdir(staged_alias):
                    shutil.rmtree(staged_alias)
                elif os.path.lexists(staged_alias):
                    os.remove(staged_alias)

                if alias_kind == "junction":
                    future_target = os.path.join(
                        destination_path,
                        relative_target,
                    )
                    result = subprocess.run(
                        [
                            "cmd.exe",
                            "/d",
                            "/c",
                            "mklink",
                            "/J",
                            staged_alias,
                            future_target,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode != 0:
                        raise OSError(
                            result.stderr.strip()
                            or result.stdout.strip()
                            or "junction creation failed"
                        )
                else:
                    if os.path.isabs(link_target):
                        link_target = os.path.join(
                            destination_path,
                            relative_target,
                        )
                    os.symlink(
                        link_target,
                        staged_alias,
                        target_is_directory=target_is_directory,
                    )
            except (OSError, TypeError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照链接重建失败 {relative_entry}: {e}",
                    include_print=False,
                )
                return False

        return True

    def _cleanup_snapshot_artifact(self, path):
        path_exists = self._snapshot_path_lexists(path)
        if path_exists is False:
            return True
        if path_exists is None:
            return False

        try:
            if self.smart_remove(path):
                return True
        except Exception as e:
            self._report_best_effort(
                "error",
                f"快照临时路径清理失败 {path}: {e}",
                include_print=False,
            )
            return False

        self._report_best_effort(
            "error",
            f"快照临时路径清理失败: {path}",
            include_print=False,
        )
        return False

    @staticmethod
    def _snapshot_path_lexists(path):
        if path is None:
            return False
        try:
            return os.path.lexists(path)
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _atomic_move_no_replace(source_path, destination_path):
        if os.name == "nt":
            os.rename(source_path, destination_path)
            return

        if sys.platform.startswith("linux"):
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                raise OSError("atomic no-replace rename is unavailable")
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source_path),
                -100,
                os.fsencode(destination_path),
                1,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    destination_path,
                )
            return

        if sys.platform == "darwin":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renamex_np = getattr(libc, "renamex_np", None)
            if renamex_np is None:
                raise OSError("atomic no-replace rename is unavailable")
            renamex_np.argtypes = (
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renamex_np.restype = ctypes.c_int
            result = renamex_np(
                os.fsencode(source_path),
                os.fsencode(destination_path),
                0x00000004,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    destination_path,
                )
            return

        raise OSError("atomic no-replace rename is unavailable")

    def _recover_old_snapshot_destination(
        self,
        old_path,
        destination_path,
    ):
        if old_path is None:
            return None, False, None, False

        old_exists = self._snapshot_path_lexists(old_path)
        destination_exists = self._snapshot_path_lexists(
            destination_path
        )
        if old_exists is False:
            return None, False, None, False
        if old_exists is None:
            return old_path, True, None, True
        if destination_exists is True:
            return old_path, True, None, False
        if destination_exists is None:
            return old_path, True, None, True

        try:
            self._atomic_move_no_replace(
                old_path,
                destination_path,
            )
        except BaseException as recovery_error:
            old_exists = self._snapshot_path_lexists(old_path)
            destination_exists = self._snapshot_path_lexists(
                destination_path
            )
            if (
                destination_exists is True
                and old_exists is False
            ):
                return None, False, recovery_error, False
            return (
                old_path,
                True,
                recovery_error,
                old_exists is None or destination_exists is None,
            )

        return None, False, None, False

    def _replace_with_staged_snapshot(
        self,
        src_path,
        dst_path,
        source_anchor=None,
        destination_anchor=None,
    ):
        destination_path = os.path.normpath(dst_path)
        parent_path = os.path.dirname(destination_path) or os.curdir
        destination_name = os.path.basename(destination_path)
        staging_path = None
        old_path = None
        preserve_old_path = False
        preserve_staging_path = False
        source_aliases = ()
        initial_destination_identity = None

        if source_anchor is None and destination_anchor is None:
            if (
                self._path_is_contained(src_path, DEFAULT_TARGET_DIR)
                and self._path_is_contained(parent_path, DEFAULT_DIR)
            ):
                source_anchor = DEFAULT_TARGET_DIR
                destination_anchor = DEFAULT_DIR
            elif (
                self._path_is_contained(src_path, DEFAULT_DIR)
                and self._path_is_contained(
                    parent_path,
                    DEFAULT_TARGET_DIR,
                )
            ):
                source_anchor = DEFAULT_DIR
                destination_anchor = DEFAULT_TARGET_DIR

        if (
            source_anchor is not None
            and not self._snapshot_ancestor_chain_is_safe(
                src_path,
                source_anchor,
            )
        ):
            return False
        if (
            destination_anchor is not None
            and not self._snapshot_ancestor_chain_is_safe(
                parent_path,
                destination_anchor,
            )
        ):
            return False
        source_is_safe, source_aliases = self._inspect_snapshot_path(
            src_path
        )
        if not source_is_safe:
            return False
        try:
            destination_exists = os.path.lexists(destination_path)
        except (OSError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照目标安全检查失败 {destination_path}: {e}",
                include_print=False,
            )
            return False
        if (
            destination_exists
            and not self._snapshot_path_is_safe(destination_path)
        ):
            return False
        if destination_exists:
            try:
                initial_destination_identity = (
                    self._ordinary_config_stat_signature(
                        os.lstat(destination_path)
                    )
                )
            except (OSError, TypeError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照目标身份检查失败 {destination_path}: {e}",
                    include_print=False,
                )
                return False

        try:
            os.makedirs(parent_path, exist_ok=True)
            if (
                source_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    src_path,
                    source_anchor,
                )
            ):
                return False
            if (
                destination_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    parent_path,
                    destination_anchor,
                )
            ):
                return False
            source_is_safe, source_aliases = self._inspect_snapshot_path(
                src_path
            )
            if not source_is_safe:
                return False
            staging_path = tempfile.mkdtemp(
                prefix=f".{destination_name}.staging-",
                dir=parent_path,
            )
            if not self.smart_copy(src_path, staging_path):
                self._report_best_effort(
                    "error",
                    f"快照暂存复制失败: {src_path} -> {staging_path}",
                    include_print=False,
                )
                return False

            source_is_safe, current_source_aliases = (
                self._inspect_snapshot_path(src_path)
            )
            if (
                not source_is_safe
                or current_source_aliases != source_aliases
            ):
                return False
            if not self._recreate_snapshot_aliases(
                staging_path,
                destination_path,
                source_aliases,
            ):
                return False

            if (
                source_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    src_path,
                    source_anchor,
                )
            ):
                return False
            if (
                destination_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    parent_path,
                    destination_anchor,
                )
            ):
                return False
            source_is_safe, current_source_aliases = (
                self._inspect_snapshot_path(src_path)
            )
            if (
                not source_is_safe
                or current_source_aliases != source_aliases
            ):
                return False
            if (
                os.path.lexists(destination_path)
                and not self._snapshot_path_is_safe(destination_path)
            ):
                return False

            if destination_exists:
                old_path = tempfile.mkdtemp(
                    prefix=f".{destination_name}.old-",
                    dir=parent_path,
                )
                os.rmdir(old_path)
                try:
                    current_destination_identity = (
                        self._ordinary_config_stat_signature(
                            os.lstat(destination_path)
                        )
                    )
                except (OSError, TypeError, ValueError):
                    current_destination_identity = None
                if (
                    current_destination_identity
                    != initial_destination_identity
                ):
                    raise OSError(
                        "snapshot destination identity changed before move"
                    )
                self._atomic_move_no_replace(
                    destination_path,
                    old_path,
                )
                try:
                    moved_destination_identity = (
                        self._ordinary_config_stat_signature(
                            os.lstat(old_path)
                        )
                    )
                except (OSError, TypeError, ValueError):
                    moved_destination_identity = None
                if (
                    moved_destination_identity
                    != initial_destination_identity
                ):
                    try:
                        self._atomic_move_no_replace(
                            old_path,
                            destination_path,
                        )
                        old_path = None
                    except BaseException:
                        preserve_old_path = True
                    raise OSError(
                        "snapshot destination identity changed during move"
                    )

            self._atomic_move_no_replace(
                staging_path,
                destination_path,
            )
            staging_path = None

            if old_path is not None:
                if not self._cleanup_snapshot_artifact(old_path):
                    preserve_old_path = True
                    return False
                old_path = None

            return True
        except Exception as e:
            (
                old_path,
                preserve_old_path,
                recovery_error,
                recovery_is_indeterminate,
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
            preserve_staging_path = recovery_is_indeterminate
            if (
                recovery_error is not None
                and not isinstance(recovery_error, Exception)
            ):
                raise recovery_error
            if recovery_error is not None:
                self._report_best_effort(
                    "error",
                    "快照替换回滚失败，旧目标保留在 "
                    f"{old_path}: {recovery_error}",
                    include_print=False,
                )
            self._report_best_effort(
                "error",
                f"快照替换失败 {src_path} -> {destination_path}: {e}",
                include_print=False,
            )
            return False
        except BaseException:
            (
                old_path,
                preserve_old_path,
                _recovery_error,
                recovery_is_indeterminate,
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
            preserve_staging_path = recovery_is_indeterminate
            raise
        finally:
            if not preserve_staging_path:
                self._cleanup_snapshot_artifact(staging_path)
            if (
                old_path is not None
                and not preserve_old_path
                and self._snapshot_path_lexists(destination_path)
            ):
                self._cleanup_snapshot_artifact(old_path)

    @staticmethod
    def _ordinary_config_stat_signature(entry_stat):
        return (
            entry_stat.st_mode,
            entry_stat.st_dev,
            entry_stat.st_ino,
            entry_stat.st_size,
            getattr(entry_stat, "st_mtime_ns", None),
            getattr(entry_stat, "st_ctime_ns", None),
            getattr(entry_stat, "st_file_attributes", None),
            getattr(entry_stat, "st_reparse_tag", None),
        )

    def _ordinary_config_reparse_kind(self, path, entry_stat):
        if not self._snapshot_entry_is_link_or_reparse(entry_stat):
            return None, None

        is_junction = bool(
            os.name == "nt"
            and (
                getattr(
                    os.path,
                    "isjunction",
                    lambda _path: False,
                )(path)
                or getattr(entry_stat, "st_reparse_tag", None)
                == getattr(
                    stat,
                    "IO_REPARSE_TAG_MOUNT_POINT",
                    0xA0000003,
                )
            )
        )
        if is_junction:
            return "junction", None
        if stat.S_ISLNK(entry_stat.st_mode):
            return "symlink", None
        return None, "unsupported_reparse"

    @staticmethod
    def _ordinary_config_file_digest(path, expected_stat):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(file_descriptor)
            expected_identity = (
                expected_stat.st_dev,
                expected_stat.st_ino,
                stat.S_IFMT(expected_stat.st_mode),
            )
            opened_identity = (
                opened_stat.st_dev,
                opened_stat.st_ino,
                stat.S_IFMT(opened_stat.st_mode),
            )
            if opened_identity != expected_identity:
                raise OSError("file identity changed while opening")

            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)

            final_stat = os.fstat(file_descriptor)
            if (
                ConfigManager._ordinary_config_stat_signature(final_stat)
                != ConfigManager._ordinary_config_stat_signature(opened_stat)
            ):
                raise OSError("file changed while hashing")
            return digest.hexdigest()
        finally:
            os.close(file_descriptor)

    @staticmethod
    @contextmanager
    def _ordinary_config_bound_directory(path, expected_stat):
        if (
            not stat.S_ISDIR(expected_stat.st_mode)
            or ConfigManager._snapshot_entry_is_link_or_reparse(
                expected_stat
            )
        ):
            raise OSError("directory binding rejected unsafe entry")

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class ByHandleFileInformation(ctypes.Structure):
                _fields_ = (
                    ("file_attributes", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME),
                    ("last_access_time", wintypes.FILETIME),
                    ("last_write_time", wintypes.FILETIME),
                    ("volume_serial_number", wintypes.DWORD),
                    ("file_size_high", wintypes.DWORD),
                    ("file_size_low", wintypes.DWORD),
                    ("number_of_links", wintypes.DWORD),
                    ("file_index_high", wintypes.DWORD),
                    ("file_index_low", wintypes.DWORD),
                )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            get_file_information = (
                kernel32.GetFileInformationByHandle
            )
            get_file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(ByHandleFileInformation),
            )
            get_file_information.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = create_file(
                path,
                0x00000001,
                0x00000001 | 0x00000002,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                information = ByHandleFileInformation()
                if not get_file_information(
                    handle,
                    ctypes.byref(information),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                file_attributes = information.file_attributes
                opened_inode = (
                    information.file_index_high << 32
                ) | information.file_index_low
                if (
                    not file_attributes & 0x00000010
                    or file_attributes & 0x00000400
                    or opened_inode != expected_stat.st_ino
                ):
                    raise OSError(
                        "directory handle identity or type changed"
                    )
                current_stat = os.lstat(path)
                if (
                    ConfigManager._ordinary_config_stat_signature(
                        current_stat
                    )
                    != ConfigManager._ordinary_config_stat_signature(
                        expected_stat
                    )
                ):
                    raise OSError(
                        "directory path identity changed after binding"
                    )
                yield path
            finally:
                close_handle(handle)
            return

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(file_descriptor)
            expected_identity = (
                expected_stat.st_dev,
                expected_stat.st_ino,
                stat.S_IFMT(expected_stat.st_mode),
            )
            opened_identity = (
                opened_stat.st_dev,
                opened_stat.st_ino,
                stat.S_IFMT(opened_stat.st_mode),
            )
            if opened_identity != expected_identity:
                raise OSError("directory identity changed while opening")
            yield file_descriptor
        finally:
            os.close(file_descriptor)

    def _ordinary_config_manifest(self, root_path, reject_reparse):
        absolute_root = os.path.abspath(root_path)
        entries = []
        root_kind = None

        def inspect_entry(current_path, relative_path):
            nonlocal root_kind
            try:
                entry_stat = os.lstat(current_path)
            except FileNotFoundError:
                return "missing_path"
            except (OSError, TypeError, ValueError):
                return "identity_check_failed"

            reparse_kind, reparse_error = (
                self._ordinary_config_reparse_kind(
                    current_path,
                    entry_stat,
                )
            )
            if reparse_error is not None:
                return reparse_error
            if reparse_kind is not None:
                if reject_reparse:
                    return "unsupported_reparse"
                try:
                    link_value = os.readlink(current_path)
                except (OSError, TypeError, ValueError):
                    return "identity_check_failed"
                entry_kind = reparse_kind
                digest = link_value
            elif stat.S_ISREG(entry_stat.st_mode):
                entry_kind = "file"
                try:
                    digest = self._ordinary_config_file_digest(
                        current_path,
                        entry_stat,
                    )
                except (OSError, TypeError, ValueError):
                    return "identity_check_failed"
            elif stat.S_ISDIR(entry_stat.st_mode):
                entry_kind = "directory"
                digest = None
            else:
                return "unsupported_type"

            if relative_path == os.curdir:
                root_kind = entry_kind
            entries.append(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    self._ordinary_config_stat_signature(entry_stat),
                    digest,
                )
            )

            if entry_kind != "directory":
                return None
            try:
                with self._ordinary_config_bound_directory(
                    current_path,
                    entry_stat,
                ) as enumeration_target:
                    with os.scandir(
                        enumeration_target
                    ) as directory_entries:
                        child_names = sorted(
                            entry.name for entry in directory_entries
                        )
                    for child_name in child_names:
                        child_relative_path = (
                            child_name
                            if relative_path == os.curdir
                            else os.path.join(
                                relative_path,
                                child_name,
                            )
                        )
                        child_error = inspect_entry(
                            os.path.join(current_path, child_name),
                            child_relative_path,
                        )
                        if child_error is not None:
                            return child_error
            except (OSError, TypeError, ValueError):
                return "identity_check_failed"
            return None

        manifest_error = inspect_entry(absolute_root, os.curdir)
        if manifest_error is not None:
            return None, manifest_error

        entries.sort(key=lambda entry: entry[0])
        return (root_kind, tuple(entries)), None

    @staticmethod
    def _ordinary_config_content_manifest(manifest):
        root_kind, entries = manifest
        return (
            root_kind,
            tuple(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    stat.S_IMODE(entry_signature[0]),
                    entry_signature[3] if entry_kind == "file" else None,
                    digest,
                )
                for (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    entry_signature,
                    digest,
                ) in entries
            ),
        )

    @staticmethod
    def _ordinary_config_stable_manifest(manifest):
        root_kind, entries = manifest
        return (
            root_kind,
            tuple(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    (
                        entry_signature[0],
                        entry_signature[1],
                        entry_signature[2],
                        entry_signature[3],
                        entry_signature[4],
                        entry_signature[6],
                        entry_signature[7],
                    ),
                    digest,
                )
                for (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    entry_signature,
                    digest,
                ) in entries
            ),
        )

    def _ordinary_config_source_state(self, source_path):
        try:
            lexical_root = os.path.abspath(CONFIG_DIR)
            lexical_path = os.path.abspath(source_path)
            resolved_root = os.path.realpath(lexical_root)
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"
        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"

        manifest, manifest_error = self._ordinary_config_manifest(
            lexical_path,
            reject_reparse=True,
        )
        if manifest_error is not None:
            return None, manifest_error
        return (
            manifest[0],
            os.path.normcase(os.path.normpath(lexical_path)),
            manifest,
        ), None

    def _ordinary_config_staging_is_safe(
        self,
        staging_path,
        expected_content_manifest,
    ):
        manifest, manifest_error = self._ordinary_config_manifest(
            staging_path,
            reject_reparse=True,
        )
        return (
            manifest_error is None
            and self._ordinary_config_content_manifest(manifest)
            == expected_content_manifest
        )

    def _ordinary_config_destination_is_current(self, destination_path):
        try:
            destination_relative_path = os.path.relpath(
                destination_path,
                DEFAULT_TARGET_DIR,
            )
        except (OSError, TypeError, ValueError):
            return False, "invalid_path"

        current_path, path_error = (
            self._resolve_ordinary_config_destination(
                destination_relative_path
            )
        )
        if path_error is not None:
            return False, path_error
        if os.path.normcase(os.path.normpath(current_path)) != os.path.normcase(
            os.path.normpath(destination_path)
        ):
            return False, "identity_check_failed"
        return True, None

    def _ordinary_config_destination_chain_paths(self, destination_path):
        try:
            trusted_root = os.path.abspath(DEFAULT_TARGET_DIR)
            parent_path = os.path.abspath(
                os.path.dirname(destination_path) or os.curdir
            )
            relative_parent = os.path.relpath(parent_path, trusted_root)
        except (OSError, TypeError, ValueError):
            return None
        if not self._path_is_contained(parent_path, trusted_root):
            return None

        chain_paths = [trusted_root]
        if relative_parent != os.curdir:
            current_path = trusted_root
            for component in relative_parent.split(os.sep):
                current_path = os.path.join(current_path, component)
                chain_paths.append(current_path)
        return tuple(chain_paths)

    def _ordinary_config_destination_binding(self, destination_path):
        chain_paths = self._ordinary_config_destination_chain_paths(
            destination_path
        )
        if chain_paths is None:
            return None, "identity_check_failed"

        chain_entries = []
        for chain_path in chain_paths:
            try:
                entry_stat = os.lstat(chain_path)
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"
            if (
                self._snapshot_entry_is_link_or_reparse(entry_stat)
                or not stat.S_ISDIR(entry_stat.st_mode)
            ):
                return None, "identity_check_failed"
            chain_entries.append(
                (
                    os.path.normcase(os.path.normpath(chain_path)),
                    (
                        stat.S_IFMT(entry_stat.st_mode),
                        entry_stat.st_dev,
                        entry_stat.st_ino,
                        getattr(
                            entry_stat,
                            "st_file_attributes",
                            None,
                        ),
                        getattr(entry_stat, "st_reparse_tag", None),
                    ),
                )
            )

        destination_exists = self._snapshot_path_lexists(destination_path)
        if destination_exists is None:
            return None, "identity_check_failed"
        destination_manifest = None
        if destination_exists:
            destination_manifest, manifest_error = (
                self._ordinary_config_manifest(
                    destination_path,
                    reject_reparse=False,
                )
            )
            if manifest_error is not None:
                return None, "identity_check_failed"
            destination_manifest = (
                self._ordinary_config_stable_manifest(
                    destination_manifest
                )
            )
        return (
            destination_exists,
            destination_manifest,
            tuple(chain_entries),
        ), None

    def _ordinary_config_binding_matches(
        self,
        destination_path,
        initial_binding,
        expected_destination,
    ):
        current_binding, binding_error = (
            self._ordinary_config_destination_binding(destination_path)
        )
        if binding_error is not None:
            return False
        if current_binding[2] != initial_binding[2]:
            return False
        expected_exists, expected_manifest = expected_destination
        return (
            current_binding[0] is expected_exists
            and current_binding[1] == expected_manifest
        )

    def _ordinary_config_acquire_chain_handles(self, destination_path):
        chain_paths = self._ordinary_config_destination_chain_paths(
            destination_path
        )
        if chain_paths is None:
            raise OSError("invalid destination parent chain")

        handles = []
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL(
                    "kernel32",
                    use_last_error=True,
                )
                create_file = kernel32.CreateFileW
                create_file.argtypes = (
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                )
                create_file.restype = wintypes.HANDLE
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                invalid_handle = ctypes.c_void_p(-1).value
                for chain_path in chain_paths:
                    handle = create_file(
                        chain_path,
                        0x80000000,
                        0x00000001 | 0x00000002,
                        None,
                        3,
                        0x02000000 | 0x00200000,
                        None,
                    )
                    if handle == invalid_handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    handles.append(("windows", handle, close_handle))
            else:
                flags = os.O_RDONLY
                flags |= getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                for chain_path in chain_paths:
                    handles.append(
                        ("posix", os.open(chain_path, flags), None)
                    )
            return handles
        except BaseException:
            self._ordinary_config_release_chain_handles(handles)
            raise

    @staticmethod
    def _ordinary_config_release_chain_handles(handles):
        for handle_kind, handle, close_handle in reversed(handles):
            try:
                if handle_kind == "windows":
                    close_handle(handle)
                else:
                    os.close(handle)
            except BaseException:
                pass

    @staticmethod
    def _reserve_sibling_artifact(parent_path, prefix):
        file_descriptor, artifact_path = tempfile.mkstemp(
            prefix=prefix,
            dir=parent_path,
        )
        os.close(file_descriptor)
        os.remove(artifact_path)
        return artifact_path

    def _replace_with_staged_ordinary_config(
        self,
        source_path,
        destination_path,
    ):
        destination_path = os.path.normpath(destination_path)
        parent_path = os.path.dirname(destination_path) or os.curdir
        destination_name = os.path.basename(destination_path)
        staging_path = None
        old_path = None
        preserve_old_path = False
        preserve_staging_path = False
        destination_existed = False
        initial_destination_binding = None
        chain_handles = []
        staged_destination_manifest = None

        source_state, source_error = (
            self._ordinary_config_source_state(source_path)
        )
        if source_error is not None:
            self._report_ordinary_config_source_error(source_error)
            return False
        expected_source_content = (
            self._ordinary_config_content_manifest(source_state[2])
        )

        destination_is_current, destination_error = (
            self._ordinary_config_destination_is_current(
                destination_path
            )
        )
        if not destination_is_current:
            self._report_ordinary_config_destination_error(
                destination_error
            )
            return False

        try:
            os.makedirs(parent_path, exist_ok=True)
            (
                initial_destination_binding,
                destination_error,
            ) = self._ordinary_config_destination_binding(
                destination_path
            )
            if destination_error is not None:
                self._report_ordinary_config_destination_error(
                    destination_error
                )
                return False
            destination_existed = initial_destination_binding[0]
            chain_handles = (
                self._ordinary_config_acquire_chain_handles(
                    destination_path
                )
            )
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                initial_destination_binding[:2],
            ):
                self._report_ordinary_config_destination_error(
                    "identity_changed"
                )
                return False

            staging_path = self._reserve_sibling_artifact(
                parent_path,
                f".{destination_name}.staging-",
            )

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False

            if not self.smart_copy(source_path, staging_path):
                self._report_best_effort(
                    "error",
                    "配置文件暂存复制失败: "
                    f"{source_path} -> {staging_path}",
                    include_print=False,
                )
                return False

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False
            if not self._ordinary_config_staging_is_safe(
                staging_path,
                expected_source_content,
            ):
                self._report_best_effort(
                    "error",
                    "配置文件暂存安全检查失败",
                    include_print=False,
                )
                return False

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False

            staged_destination_manifest, staging_error = (
                self._ordinary_config_manifest(
                    staging_path,
                    reject_reparse=True,
                )
            )
            if staging_error is not None:
                self._report_best_effort(
                    "error",
                    "配置文件暂存安全检查失败",
                    include_print=False,
                )
                return False
            staged_destination_manifest = (
                self._ordinary_config_stable_manifest(
                    staged_destination_manifest
                )
            )

            destination_is_current, destination_error = (
                self._ordinary_config_destination_is_current(
                    destination_path
                )
            )
            if not destination_is_current:
                self._report_ordinary_config_destination_error(
                    destination_error
                )
                return False
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                initial_destination_binding[:2],
            ):
                self._report_ordinary_config_destination_error(
                    "identity_changed"
                )
                return False

            if destination_existed:
                old_path = self._reserve_sibling_artifact(
                    parent_path,
                    f".{destination_name}.old-",
                )
                if not self._ordinary_config_binding_matches(
                    destination_path,
                    initial_destination_binding,
                    initial_destination_binding[:2],
                ):
                    raise OSError(
                        "destination changed before recovery transition"
                    )
                self._atomic_move_no_replace(
                    destination_path,
                    old_path,
                )
                if not self._ordinary_config_binding_matches(
                    destination_path,
                    initial_destination_binding,
                    (False, None),
                ):
                    raise OSError(
                        "destination parent changed during recovery transition"
                    )
                old_manifest, old_error = self._ordinary_config_manifest(
                    old_path,
                    reject_reparse=False,
                )
                if (
                    old_error is not None
                    or self._ordinary_config_stable_manifest(
                        old_manifest
                    )
                    != initial_destination_binding[1]
                ):
                    try:
                        self._atomic_move_no_replace(
                            old_path,
                            destination_path,
                        )
                        old_path = None
                    except BaseException:
                        preserve_old_path = True
                    raise OSError(
                        "old destination identity changed during transition"
                    )

            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                (False, None),
            ):
                raise OSError(
                    "destination changed before staging transition"
                )
            self._atomic_move_no_replace(
                staging_path,
                destination_path,
            )
            staging_path = None
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                (True, staged_destination_manifest),
            ):
                raise OSError(
                    "destination changed during staging transition"
                )

            if old_path is not None:
                if not self._cleanup_snapshot_artifact(old_path):
                    preserve_old_path = True
                    raise OSError("旧配置清理失败")
                old_path = None

            return True
        except BaseException as replacement_error:
            recovery_error = None
            replacement_artifact = staging_path
            old_exists = self._snapshot_path_lexists(old_path)
            destination_exists = self._snapshot_path_lexists(
                destination_path
            )

            if old_exists is None or destination_exists is None:
                preserve_old_path = old_path is not None
                preserve_staging_path = replacement_artifact is not None
            elif old_exists is True:
                try:
                    if destination_exists is True:
                        if (
                            initial_destination_binding is None
                            or staged_destination_manifest is None
                            or not self._ordinary_config_binding_matches(
                                destination_path,
                                initial_destination_binding,
                                (
                                    True,
                                    staged_destination_manifest,
                                ),
                            )
                        ):
                            raise OSError(
                                "replacement destination identity is indeterminate"
                            )
                        replacement_exists = (
                            self._snapshot_path_lexists(
                                replacement_artifact
                            )
                        )
                        if replacement_exists is None:
                            raise OSError(
                                "replacement artifact state is indeterminate"
                            )
                        if replacement_artifact is None:
                            replacement_artifact = (
                                self._reserve_sibling_artifact(
                                    parent_path,
                                    f".{destination_name}.staging-",
                                )
                            )
                        elif replacement_exists:
                            replacement_artifact = (
                                self._reserve_sibling_artifact(
                                    parent_path,
                                    f".{destination_name}.staging-",
                                )
                            )
                        self._atomic_move_no_replace(
                            destination_path,
                            replacement_artifact,
                        )
                        if not self._ordinary_config_binding_matches(
                            destination_path,
                            initial_destination_binding,
                            (False, None),
                        ):
                            raise OSError(
                                "destination parent changed during recovery"
                            )
                    old_manifest, old_error = (
                        self._ordinary_config_manifest(
                            old_path,
                            reject_reparse=False,
                        )
                    )
                    if (
                        initial_destination_binding is None
                        or old_error is not None
                        or self._ordinary_config_stable_manifest(
                            old_manifest
                        )
                        != initial_destination_binding[1]
                    ):
                        raise OSError(
                            "old destination identity changed before recovery"
                        )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        (False, None),
                    ):
                        raise OSError(
                            "destination changed before recovery"
                        )
                    self._atomic_move_no_replace(
                        old_path,
                        destination_path,
                    )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        initial_destination_binding[:2],
                    ):
                        raise OSError(
                            "destination changed during recovery"
                        )
                    old_path = None
                    preserve_old_path = False
                except BaseException as error:
                    recovery_error = error
                    preserve_old_path = True
            elif (
                not destination_existed
                and destination_exists is True
            ):
                try:
                    if (
                        initial_destination_binding is None
                        or staged_destination_manifest is None
                        or not self._ordinary_config_binding_matches(
                            destination_path,
                            initial_destination_binding,
                            (True, staged_destination_manifest),
                        )
                    ):
                        raise OSError(
                            "new destination identity is indeterminate"
                        )
                    replacement_exists = self._snapshot_path_lexists(
                        replacement_artifact
                    )
                    if replacement_exists is None:
                        raise OSError(
                            "replacement artifact state is indeterminate"
                        )
                    if (
                        replacement_artifact is None
                        or replacement_exists
                    ):
                        replacement_artifact = (
                            self._reserve_sibling_artifact(
                                parent_path,
                                f".{destination_name}.staging-",
                            )
                        )
                    self._atomic_move_no_replace(
                        destination_path,
                        replacement_artifact,
                    )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        (False, None),
                    ):
                        raise OSError(
                            "destination parent changed during recovery"
                        )
                except BaseException as error:
                    recovery_error = error

            staging_path = replacement_artifact
            if recovery_error is not None:
                self._report_best_effort(
                    "error",
                    "配置文件替换回滚失败，恢复数据保留在 "
                    f"{old_path}: {recovery_error}",
                    include_print=False,
                )
            self._report_best_effort(
                "error",
                "配置文件暂存替换失败 "
                f"{source_path} -> {destination_path}: "
                f"{replacement_error}",
                include_print=False,
            )
            if not isinstance(replacement_error, Exception):
                raise replacement_error
            if (
                recovery_error is not None
                and not isinstance(recovery_error, Exception)
            ):
                raise recovery_error
            return False
        finally:
            try:
                if not preserve_staging_path:
                    self._cleanup_snapshot_artifact(staging_path)
                if (
                    old_path is not None
                    and not preserve_old_path
                    and self._snapshot_path_lexists(destination_path)
                    is True
                ):
                    self._cleanup_snapshot_artifact(old_path)
            finally:
                self._ordinary_config_release_chain_handles(
                    chain_handles
                )

    def smart_remove(self, path):
        """
        智能删除方法，自动判断是文件还是目录

        Args:
            path (str): 要删除的路径

        Returns:
            bool: 删除是否成功
        """
        try:
            path_exists = self._snapshot_path_lexists(path)
            if path_exists is False:
                self._report_best_effort(
                    "warning",
                    f"路径不存在: {path}",
                    include_print=False,
                )
                return True  # 不存在也算"删除成功"
            if path_exists is None:
                self._report_best_effort(
                    "error",
                    f"无法确定路径是否存在: {path}",
                    include_print=False,
                )
                return False

            entry_stat = os.lstat(path)
            if self._snapshot_entry_is_link_or_reparse(entry_stat):
                is_junction = bool(
                    os.name == "nt"
                    and (
                        getattr(
                            os.path,
                            "isjunction",
                            lambda _path: False,
                        )(path)
                        or getattr(entry_stat, "st_reparse_tag", None)
                        == getattr(
                            stat,
                            "IO_REPARSE_TAG_MOUNT_POINT",
                            0xA0000003,
                        )
                    )
                )
                if (
                    is_junction
                    or stat.S_ISDIR(entry_stat.st_mode)
                ):
                    os.rmdir(path)
                else:
                    os.remove(path)
                self._report_best_effort(
                    "info",
                    f"链接或重解析点删除成功: {path}",
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                # 删除文件
                os.remove(path)
                self._report_best_effort(
                    "info",
                    f"文件删除成功: {path}",
                )
            elif stat.S_ISDIR(entry_stat.st_mode):
                # 删除目录及其内容
                shutil.rmtree(path)
                self._report_best_effort(
                    "info",
                    f"目录删除成功: {path}",
                )
            else:
                self._report_best_effort(
                    "warning",
                    f"未知的路径类型: {path}",
                    include_print=False,
                )
                return False

            return True

        except PermissionError as e:
            self._report_best_effort(
                "error",
                f"权限错误，无法删除 {path}: {e}",
            )
            return False
        except Exception as e:
            self._report_best_effort(
                "error",
                f"删除失败 {path}: {e}",
            )
            return False

    def init_upgrade_version_list(self):
        print("初始化升级版本列表")
        self.logger.info("初始化升级版本列表")
        for version in ALL_VERSIONS:
            if version > TARGET_VERSION:
                break
            if version <= self.current_version:
                continue
            self.upgrade_version_list.append(version)

        self.logger.info(f"升级版本列表：{self.upgrade_version_list}")

    def _ordinary_config_path_error(self, relative_path):
        if not isinstance(relative_path, str):
            return "non_string_path"
        if not relative_path.strip():
            return "empty_path"
        if "\x00" in relative_path:
            return "nul_path"
        if relative_path.startswith(("/", "\\")):
            return "rooted_path"
        if ntpath.splitdrive(relative_path)[0]:
            return "drive_qualified_path"
        if os.path.isabs(relative_path):
            return "absolute_path"
        return None

    def _destination_entry_state(self, path):
        try:
            path_lexists = os.path.lexists(path)
        except (OSError, ValueError):
            return None, "identity_check_failed"

        try:
            os.lstat(path)
        except FileNotFoundError:
            if path_lexists:
                return None, "identity_check_failed"
            return False, None
        except (OSError, ValueError):
            return None, "identity_check_failed"

        try:
            if not os.path.exists(path):
                return None, "dangling_link"
        except (OSError, ValueError):
            return None, "identity_check_failed"
        return True, None

    def _resolve_ordinary_config_destination(self, relative_path):
        path_error = self._ordinary_config_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            destination_path = os.path.abspath(
                os.path.join(DEFAULT_TARGET_DIR, relative_path)
            )
            lexical_roots = tuple(
                os.path.abspath(root)
                for root in (
                    UI_CONFIG_PATH,
                    UI_PIC_PATH,
                    AI_MODEL_CONFIG_PATH,
                    SCANNER_BARCODE_CONFIG_PATH,
                )
            )
        except (OSError, ValueError):
            return None, "invalid_path"

        lexical_root = next(
            (
                root
                for root in lexical_roots
                if self._path_is_contained(destination_path, root)
            ),
            None,
        )
        if lexical_root is None:
            return None, "outside_allowed_root"

        _, state_error = self._destination_entry_state(destination_path)
        if state_error is not None:
            return None, state_error

        try:
            resolved_root = os.path.realpath(lexical_root)
            resolved_destination = os.path.realpath(destination_path)
            resolved_parent = os.path.realpath(
                os.path.dirname(destination_path)
            )
        except (OSError, ValueError):
            return None, "invalid_path"

        destination_is_managed_root = os.path.normcase(
            os.path.normpath(destination_path)
        ) == os.path.normcase(os.path.normpath(lexical_root))
        if (
            not self._path_is_contained(resolved_destination, resolved_root)
            or (
                not destination_is_managed_root
                and not self._path_is_contained(resolved_parent, resolved_root)
            )
        ):
            return None, "symlink_escape"
        return destination_path, None

    def _report_ordinary_config_destination_error(self, error):
        message = f"ordinary_config error: destination_path:{error}"
        self._report_best_effort("error", message)

    def _ordinary_config_source_parent_chain_state(self, lexical_root):
        try:
            trusted_root = os.path.abspath(DEFAULT_DIR)
            expected_root = os.path.abspath(
                os.path.join(trusted_root, "resources", "config")
            )
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if os.path.normcase(os.path.normpath(lexical_root)) != os.path.normcase(
            os.path.normpath(expected_root)
        ):
            return None, "outside_allowed_root"

        chain = []
        for parent_path in (
            trusted_root,
            os.path.join(trusted_root, "resources"),
            expected_root,
        ):
            try:
                lexical_exists = os.path.lexists(parent_path)
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"

            try:
                parent_stat = os.lstat(parent_path)
            except FileNotFoundError:
                if lexical_exists is False:
                    return None, "missing_parent"
                return None, "identity_check_failed"
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"

            if lexical_exists is False:
                return None, "identity_check_failed"
            if self._snapshot_entry_is_link_or_reparse(parent_stat):
                return None, "symlink_escape"
            if not stat.S_ISDIR(parent_stat.st_mode):
                return None, "identity_check_failed"

            chain.append(
                (
                    os.path.normcase(os.path.normpath(parent_path)),
                    parent_stat.st_dev,
                    parent_stat.st_ino,
                    parent_stat.st_mode,
                    getattr(parent_stat, "st_file_attributes", None),
                    getattr(parent_stat, "st_reparse_tag", None),
                )
            )

        return tuple(chain), None

    def _ordinary_config_source_entry_state(self, lexical_path):
        try:
            lexical_exists = os.path.lexists(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "identity_check_failed"

        try:
            source_stat = os.lstat(lexical_path)
        except FileNotFoundError:
            if lexical_exists is False:
                return None, "missing_path"
            return None, "identity_check_failed"
        except (OSError, TypeError, ValueError):
            return None, "identity_check_failed"

        if lexical_exists is False:
            return None, "identity_check_failed"
        if self._snapshot_entry_is_link_or_reparse(source_stat):
            if stat.S_ISLNK(source_stat.st_mode):
                try:
                    os.stat(lexical_path)
                except FileNotFoundError:
                    return None, "dangling_link"
                except (OSError, TypeError, ValueError):
                    return None, "identity_check_failed"
            return None, "unsupported_type"
        if not (
            stat.S_ISREG(source_stat.st_mode)
            or stat.S_ISDIR(source_stat.st_mode)
        ):
            return None, "unsupported_type"
        return source_stat, None

    def _resolve_ordinary_config_source(self, destination_path):
        try:
            lexical_root = os.path.abspath(CONFIG_DIR)
            source_name = os.path.basename(os.path.normpath(destination_path))
            lexical_path = os.path.abspath(
                os.path.join(lexical_root, source_name)
            )
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"

        parent_chain, parent_error = (
            self._ordinary_config_source_parent_chain_state(lexical_root)
        )
        if parent_error is not None:
            return None, parent_error

        try:
            resolved_root = os.path.realpath(lexical_root)
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"

        _, source_error = self._ordinary_config_source_entry_state(
            lexical_path
        )

        confirmed_parent_chain, confirmed_parent_error = (
            self._ordinary_config_source_parent_chain_state(lexical_root)
        )
        if confirmed_parent_error is not None:
            return None, confirmed_parent_error
        if confirmed_parent_chain != parent_chain:
            return None, "identity_check_failed"

        if source_error == "missing_path":
            return lexical_path, source_error
        if source_error is not None:
            return None, source_error
        return lexical_path, None

    def _report_ordinary_config_source_error(self, error):
        message = f"ordinary_config error: source_path:{error}"
        self._report_best_effort("error", message)

    def backup_config_file(self, config_file_source_path):
        managed_roots = (
            (
                AI_MODEL_CONFIG_PATH,
                BACKUP_AI_MODEL_CONFIG_PATH,
                "ai_model_config_flag",
            ),
            (
                UI_CONFIG_PATH,
                BACKUP_UI_CONFIG_PATH,
                "ui_config_flag",
            ),
            (
                UI_PIC_PATH,
                BACKUP_UI_PIC_PATH,
                "ui_pic_flag",
            ),
            (
                SCANNER_BARCODE_CONFIG_PATH,
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                "scannre_barcode_config_flag",
            ),
        )

        for file_path in config_file_source_path:
            file_path, path_error = self._resolve_ordinary_config_destination(
                file_path
            )
            if path_error is not None:
                self._report_ordinary_config_destination_error(path_error)
                return False
            managed_root = next(
                (
                    (backup_source_path, backup_path, flag_name)
                    for backup_source_path, backup_path, flag_name in managed_roots
                    if self.is_parent_path_os(backup_source_path, file_path)
                ),
                None,
            )
            if managed_root is None:
                message = f"配置文件不在可备份目录中: {file_path}"
                self._report_best_effort("error", message)
                return False

            backup_source_path, backup_path, flag_name = managed_root
            if getattr(self, flag_name) is False:
                continue

            if not self._snapshot_ancestor_chain_is_safe(
                backup_source_path,
                DEFAULT_TARGET_DIR,
            ):
                message = (
                    "配置文件备份安全检查失败: "
                    f"{backup_source_path}"
                )
                self._report_best_effort("error", message)
                return False
            if not self._snapshot_ancestor_chain_is_safe(
                os.path.dirname(os.path.normpath(backup_path)),
                DEFAULT_DIR,
            ):
                message = (
                    "配置文件备份目标安全检查失败: "
                    f"{backup_path}"
                )
                self._report_best_effort("error", message)
                return False
            if not self._snapshot_path_is_safe(backup_source_path):
                message = (
                    "配置文件备份安全检查失败: "
                    f"{backup_source_path}"
                )
                self._report_best_effort("error", message)
                return False

            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                if not self._replace_with_staged_snapshot(
                    backup_source_path,
                    backup_path,
                ):
                    message = f"配置文件备份失败: {backup_source_path} -> {backup_path}"
                    self._report_best_effort("error", message)
                    return False
            except Exception as e:
                self._report_best_effort(
                    "error",
                    f"备份配置文件时发生错误：{e}",
                )
                return False

            setattr(self, flag_name, False)
            self._report_best_effort(
                "info",
                f"配置文件备份已保存到 {backup_path}",
            )
        return True

    def _normalize_json_merge_entries(self, raw_entries):
        if not isinstance(raw_entries, list):
            return None, "section_must_be_list"

        normalized_entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                return None, "entry_must_be_object"
            if set(entry) != {"target", "source"}:
                return None, "entry_fields_invalid"

            target = entry["target"]
            if not isinstance(target, str) or not target.strip():
                return None, "target_must_be_non_empty_string"

            source = entry["source"]
            if not isinstance(source, str) or not source.strip():
                return None, "source_must_be_non_empty_string"

            normalized_entries.append(
                {
                    "target": target,
                    "source": source,
                }
            )

        return normalized_entries, None

    def _relative_json_merge_path_error(self, relative_path):
        if "\x00" in relative_path:
            return "invalid_path"
        if os.path.isabs(relative_path) or os.path.splitdrive(relative_path)[0]:
            return "absolute_path"

        path_components = relative_path.replace("\\", "/").split("/")
        if ".." in path_components:
            return "parent_reference"
        return None

    def _path_is_contained(self, path, root):
        try:
            normalized_path = os.path.normcase(os.path.abspath(path))
            normalized_root = os.path.normcase(os.path.abspath(root))
            return (
                os.path.commonpath([normalized_root, normalized_path])
                == normalized_root
            )
        except (OSError, ValueError):
            return False

    def _resolve_json_merge_target(self, relative_path):
        path_error = self._relative_json_merge_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            lexical_path = os.path.abspath(
                os.path.join(DEFAULT_TARGET_DIR, relative_path)
            )
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, ValueError):
            return None, "invalid_path"

        allowed_roots = (
            UI_CONFIG_PATH,
            AI_MODEL_CONFIG_PATH,
            SCANNER_BARCODE_CONFIG_PATH,
        )
        for allowed_root in allowed_roots:
            lexical_root = os.path.abspath(allowed_root)
            if not self._path_is_contained(lexical_path, lexical_root):
                continue

            try:
                resolved_root = os.path.realpath(lexical_root)
            except (OSError, ValueError):
                return None, "invalid_path"
            if not self._path_is_contained(resolved_path, resolved_root):
                return None, "symlink_escape"
            try:
                if not os.path.isfile(lexical_path):
                    return None, "missing_file"
            except OSError:
                return None, "missing_file"
            return lexical_path, None

        return None, "outside_allowed_root"

    def _resolve_json_merge_source(self, relative_path):
        path_error = self._relative_json_merge_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            lexical_path = os.path.abspath(os.path.join(DEFAULT_DIR, relative_path))
            lexical_root = os.path.abspath(CONFIG_DIR)
            resolved_path = os.path.realpath(lexical_path)
            resolved_root = os.path.realpath(lexical_root)
        except (OSError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"
        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"
        try:
            if not os.path.isfile(lexical_path):
                return None, "missing_file"
        except OSError:
            return None, "missing_file"
        return lexical_path, None

    def _json_merge_manifest_path_key(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.strip():
            return None
        if self._relative_json_merge_path_error(relative_path) is not None:
            return None
        try:
            return os.path.normcase(
                os.path.normpath(
                    os.path.abspath(
                        os.path.join(DEFAULT_TARGET_DIR, relative_path)
                    )
                )
            )
        except (OSError, ValueError):
            return None

    def _destination_identity_key(self, path, path_exists):
        try:
            absolute_path = os.path.abspath(path)
            if path_exists:
                resolved_path = os.path.realpath(absolute_path)
            else:
                resolved_parent = os.path.realpath(
                    os.path.dirname(absolute_path)
                )
                resolved_path = os.path.join(
                    resolved_parent,
                    os.path.basename(absolute_path),
                )
            return os.path.normcase(os.path.normpath(resolved_path)), None
        except (OSError, ValueError):
            return None, "identity_check_failed"

    def _destination_paths_are_aliases(self, first_path, second_path):
        first_exists, first_state_error = self._destination_entry_state(first_path)
        if first_state_error is not None:
            return False, first_state_error
        second_exists, second_state_error = self._destination_entry_state(second_path)
        if second_state_error is not None:
            return False, second_state_error

        try:
            if first_exists and second_exists:
                if os.path.samefile(first_path, second_path):
                    return True, None
        except (OSError, ValueError):
            return False, "identity_check_failed"

        first_key, first_key_error = self._destination_identity_key(
            first_path,
            first_exists,
        )
        if first_key_error is not None:
            return False, first_key_error
        second_key, second_key_error = self._destination_identity_key(
            second_path,
            second_exists,
        )
        if second_key_error is not None:
            return False, second_key_error
        return (
            first_key is not None
            and second_key is not None
            and first_key == second_key
        ), None

    def _report_json_merge(self, level, message):
        print(message)
        getattr(self.logger, level)(message)

    def _preflight_json_merge_conflicts(self, entries, config_entries):
        config_target_paths = []
        for config_entry in config_entries:
            target_path, path_error = (
                self._resolve_ordinary_config_destination(config_entry)
            )
            if path_error is not None:
                self._report_ordinary_config_destination_error(path_error)
                return False
            config_target_paths.append(target_path)

        for entry in entries:
            target_path = self._json_merge_manifest_path_key(entry["target"])
            if target_path is None:
                continue
            for config_target_path in config_target_paths:
                paths_are_aliases, identity_error = (
                    self._destination_paths_are_aliases(
                        target_path,
                        config_target_path,
                    )
                )
                if identity_error is not None:
                    error_category = (
                        "destination_alias_dangling_link"
                        if identity_error == "dangling_link"
                        else "destination_identity_check_failed"
                    )
                    self._report_json_merge(
                        "error",
                        f"json_merge error: {error_category}",
                    )
                    return False
                if not paths_are_aliases:
                    continue
                self._report_json_merge(
                    "error",
                    "json_merge error: target_conflicts_with_config",
                )
                return False
        return True

    def _apply_json_merges(self, entries):
        for entry in entries:
            target_relative_path = entry["target"]
            source_relative_path = entry["source"]

            target_path, target_error = self._resolve_json_merge_target(
                target_relative_path
            )
            if target_error is not None:
                self._report_json_merge(
                    "error",
                    f"json_merge error: target_path:{target_error}",
                )
                return False

            source_path, source_error = self._resolve_json_merge_source(
                source_relative_path
            )
            if source_error is not None:
                self._report_json_merge(
                    "error",
                    f"json_merge error: source_path:{source_error}",
                )
                return False

            try:
                paths_are_aliases = os.path.samefile(target_path, source_path)
            except OSError:
                self._report_json_merge(
                    "error",
                    "json_merge error: target_source_alias_check_failed",
                )
                return False
            if paths_are_aliases:
                self._report_json_merge(
                    "error",
                    "json_merge error: target_source_alias",
                )
                return False

            self._report_json_merge(
                "info",
                "json_merge start: "
                f"target={target_relative_path} source={source_relative_path}",
            )
            if not self.backup_config_file([target_relative_path]):
                self._report_json_merge(
                    "error",
                    "json_merge error: backup_failed",
                )
                return False

            merged, reason = merge_json_files_with_reason(target_path, source_path)
            if not merged:
                self._report_json_merge(
                    "error",
                    f"json_merge error: merge_failed:{reason}",
                )
                return False

            self._report_json_merge(
                "info",
                "json_merge success: "
                f"target={target_relative_path} source={source_relative_path}",
            )

        return True

    def update_config_file(self):
        self._report_best_effort("info", "正在更新配置文件...")
        try:
            for upgrade_version in self.upgrade_version_list:
                target_version_path = DEFAULT_DIR + f"resources/version_{upgrade_version}.yml"
                data = Readyml.load_yaml(target_version_path)
                config_file_source_path = data["config"]

                json_merge_entries, schema_error = (
                    self._normalize_json_merge_entries(
                        data.get("json_merge", [])
                    )
                )
                if schema_error is not None:
                    self._report_json_merge(
                        "error",
                        f"json_merge schema error: {schema_error}",
                    )
                    return False
                if not self._preflight_json_merge_conflicts(
                    json_merge_entries,
                    config_file_source_path,
                ):
                    return False

                config_updates = []
                for config_file_path in config_file_source_path:
                    target_file_path, path_error = (
                        self._resolve_ordinary_config_destination(
                            config_file_path
                        )
                    )
                    if path_error is not None:
                        self._report_ordinary_config_destination_error(
                            path_error
                        )
                        return False
                    new_config_file_path, source_error = (
                        self._resolve_ordinary_config_source(
                            target_file_path
                        )
                    )
                    if source_error is not None:
                        self._report_ordinary_config_source_error(
                            source_error
                        )
                        return False
                    config_updates.append(
                        (
                            config_file_path,
                            new_config_file_path,
                            target_file_path,
                        )
                    )

                if self.backup_config_file(config_file_source_path):
                    for (
                        config_file_path,
                        new_config_file_path,
                        target_file_path,
                    ) in config_updates:
                        self._report_best_effort(
                            "info",
                            f"更新配置文件: {config_file_path}",
                        )
                        (
                            new_config_file_path,
                            source_error,
                        ) = self._resolve_ordinary_config_source(
                            target_file_path
                        )
                        if source_error is not None:
                            self._report_ordinary_config_source_error(
                                source_error
                            )
                            return False
                        if not self._replace_with_staged_ordinary_config(
                            new_config_file_path,
                            target_file_path,
                        ):
                            self._report_best_effort(
                                "error",
                                "配置文件升级失败",
                            )
                            return False
                        self._report_best_effort(
                            "info",
                            "配置文件升级成功",
                        )
                else:
                    self._report_best_effort(
                        "error",
                        "配置文件备份错误",
                    )
                    return False

                if not self._apply_json_merges(json_merge_entries):
                    return False
            return True
        except Exception as e:
            self._report_best_effort(
                "error",
                f"配置文件升级失败: {e}",
            )
            return False

    def restore_config(self):
        restore_targets = (
            (
                "ui_pic_flag",
                BACKUP_UI_PIC_PATH,
                UI_PIC_PATH,
                "ui图片配置文件",
            ),
            (
                "ai_model_config_flag",
                BACKUP_AI_MODEL_CONFIG_PATH,
                AI_MODEL_CONFIG_PATH,
                "ai模型配置文件",
            ),
            (
                "scannre_barcode_config_flag",
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                SCANNER_BARCODE_CONFIG_PATH,
                "条码扫描配置文件",
            ),
            (
                "ui_config_flag",
                BACKUP_UI_CONFIG_PATH,
                UI_CONFIG_PATH,
                "UI配置文件",
            ),
        )
        restore_succeeded = True

        for flag_name, backup_path, target_path, display_name in restore_targets:
            if getattr(self, flag_name) is not False:
                continue

            try:
                copy_succeeded = self._replace_with_staged_snapshot(
                    backup_path,
                    target_path,
                )
            except Exception as e:
                copy_succeeded = False
                self._report_best_effort(
                    "error",
                    f"{display_name}恢复失败: {e}",
                )

            if copy_succeeded:
                self._report_best_effort(
                    "info",
                    f"{display_name}已恢复",
                )
            else:
                restore_succeeded = False
                message = f"{display_name}恢复失败: {backup_path} -> {target_path}"
                self._report_best_effort("error", message)

        if not restore_succeeded:
            self._report_best_effort(
                "error",
                "配置文件恢复失败，保留备份文件",
            )
            return False

        self._report_best_effort("info", "配置文件已恢复")
        return self.delete_backup_file()

    def delete_backup_file(self):
        self._report_best_effort("info", "正在删除备份文件...")
        backup_targets = (
            ("ui_pic_flag", BACKUP_UI_PIC_PATH, "ui图片配置备份文件"),
            (
                "ai_model_config_flag",
                BACKUP_AI_MODEL_CONFIG_PATH,
                "ai模型配置备份文件",
            ),
            (
                "scannre_barcode_config_flag",
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                "条码扫描配置备份文件",
            ),
            ("ui_config_flag", BACKUP_UI_CONFIG_PATH, "UI配置备份文件"),
        )
        delete_succeeded = True

        for flag_name, backup_path, display_name in backup_targets:
            if getattr(self, flag_name) is not False:
                continue

            try:
                removed = self.smart_remove(backup_path)
            except Exception as e:
                removed = False
                self._report_best_effort(
                    "error",
                    f"{display_name}删除失败: {e}",
                )

            if removed:
                self._report_best_effort(
                    "info",
                    f"{display_name}已删除",
                )
            else:
                delete_succeeded = False
                message = f"{display_name}删除失败，保留备份: {backup_path}"
                self._report_best_effort("error", message)

        return delete_succeeded


if __name__ == "__main__":
    from log_model.log_manager import LogManager

    log_manager = LogManager()
    config_manager = ConfigManager(log_manager, "0.25.06.01")
    config_manager.update_config_file()
