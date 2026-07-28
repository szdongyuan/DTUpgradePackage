import ntpath
import os
import shutil
import stat
import subprocess
import sys
import tempfile

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
            except Exception:
                pass
        try:
            getattr(self.logger, level)(message)
        except Exception:
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
            self.logger.error(
                f"快照祖先安全检查失败 {path}: {e}"
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
                self.logger.error(
                    f"快照祖先安全检查失败 {current_path}: {e}"
                )
                return False

            try:
                resolved_path = os.path.realpath(current_path)
                if not self._path_is_contained(
                    resolved_path,
                    resolved_anchor,
                ):
                    self.logger.error(
                        f"快照祖先逃逸可信目录: {current_path}"
                    )
                    return False

                followed_stat = os.stat(current_path)
                identity = (
                    followed_stat.st_dev,
                    followed_stat.st_ino,
                )
            except (OSError, ValueError) as e:
                self.logger.error(
                    f"快照祖先安全检查失败 {current_path}: {e}"
                )
                return False

            if identity in visited_identities:
                self.logger.error(
                    f"快照祖先循环链接: {current_path}"
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
            self.logger.error(f"快照安全检查失败 {root_path}: {e}")
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
                self.logger.error(
                    f"快照安全检查失败 {current_path}: {e}"
                )
                return False, ()

            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except (OSError, ValueError) as e:
                    self.logger.error(
                        f"快照安全检查失败 {entry.path}: {e}"
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
                        self.logger.error(
                            f"快照拒绝不支持的重解析点: {entry.path}"
                        )
                        return False, ()

                    try:
                        resolved_target = os.path.realpath(entry.path)
                        if not self._path_is_contained(
                            resolved_target,
                            resolved_root,
                        ):
                            self.logger.error(
                                f"快照拒绝外部链接或重解析点: {entry.path}"
                            )
                            return False, ()

                        followed_stat = os.stat(entry.path)
                        target_identity = (
                            followed_stat.st_dev,
                            followed_stat.st_ino,
                        )
                    except (OSError, ValueError) as e:
                        self.logger.error(
                            f"快照链接安全检查失败 {entry.path}: {e}"
                        )
                        return False, ()

                    target_is_directory = stat.S_ISDIR(
                        followed_stat.st_mode
                    )
                    if (
                        target_is_directory
                        and target_identity in ancestor_identities
                    ):
                        self.logger.error(
                            f"快照拒绝循环链接或重解析点: {entry.path}"
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
                        self.logger.error(
                            f"快照链接安全检查失败 {entry.path}: {e}"
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
                    self.logger.error(
                        f"快照拒绝目录循环: {entry.path}"
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
                self.logger.error(
                    f"快照链接重建失败 {relative_entry}: {e}"
                )
                return False

        return True

    def _cleanup_snapshot_artifact(self, path):
        if not path or not os.path.exists(path):
            return True

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
        try:
            return os.path.lexists(path)
        except (OSError, ValueError):
            return None

    def _recover_old_snapshot_destination(
        self,
        old_path,
        destination_path,
    ):
        if old_path is None:
            return None, False, None

        old_exists = self._snapshot_path_lexists(old_path)
        destination_exists = self._snapshot_path_lexists(
            destination_path
        )
        if not old_exists:
            return None, False, None
        if destination_exists:
            return old_path, True, None
        if destination_exists is None:
            return old_path, True, None

        try:
            os.replace(old_path, destination_path)
        except BaseException as recovery_error:
            old_exists = self._snapshot_path_lexists(old_path)
            destination_exists = self._snapshot_path_lexists(
                destination_path
            )
            if destination_exists and not old_exists:
                return None, False, recovery_error
            return old_path, True, recovery_error

        return None, False, None

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
        source_aliases = ()

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
            self.logger.error(
                f"快照目标安全检查失败 {destination_path}: {e}"
            )
            return False
        if (
            destination_exists
            and not self._snapshot_path_is_safe(destination_path)
        ):
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
                self.logger.error(
                    f"快照暂存复制失败: {src_path} -> {staging_path}"
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

            if os.path.exists(destination_path):
                old_path = tempfile.mkdtemp(
                    prefix=f".{destination_name}.old-",
                    dir=parent_path,
                )
                os.rmdir(old_path)
                os.replace(destination_path, old_path)

            os.replace(staging_path, destination_path)
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
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
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
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
            raise
        finally:
            self._cleanup_snapshot_artifact(staging_path)
            if (
                old_path is not None
                and not preserve_old_path
                and self._snapshot_path_lexists(destination_path)
            ):
                self._cleanup_snapshot_artifact(old_path)

    def smart_remove(self, path):
        """
        智能删除方法，自动判断是文件还是目录

        Args:
            path (str): 要删除的路径

        Returns:
            bool: 删除是否成功
        """
        try:
            if not os.path.exists(path):
                self._report_best_effort(
                    "warning",
                    f"路径不存在: {path}",
                    include_print=False,
                )
                return True  # 不存在也算"删除成功"

            if os.path.isfile(path):
                # 删除文件
                os.remove(path)
                self._report_best_effort(
                    "info",
                    f"文件删除成功: {path}",
                )
            elif os.path.isdir(path):
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
        print(message)
        self.logger.error(message)

    def _resolve_ordinary_config_source(self, destination_path):
        try:
            lexical_root = os.path.abspath(CONFIG_DIR)
            source_name = os.path.basename(os.path.normpath(destination_path))
            lexical_path = os.path.abspath(
                os.path.join(lexical_root, source_name)
            )
            resolved_root = os.path.realpath(lexical_root)
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"
        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"

        try:
            if not os.path.exists(lexical_path):
                return None, "missing_path"
            if not (
                os.path.isfile(lexical_path)
                or os.path.isdir(lexical_path)
            ):
                return None, "unsupported_type"
        except OSError:
            return None, "invalid_path"
        return lexical_path, None

    def _report_ordinary_config_source_error(self, error):
        message = f"ordinary_config error: source_path:{error}"
        print(message)
        self.logger.error(message)

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
                print(message)
                self.logger.error(message)
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
                print(message)
                self.logger.error(message)
                return False
            if not self._snapshot_ancestor_chain_is_safe(
                os.path.dirname(os.path.normpath(backup_path)),
                DEFAULT_DIR,
            ):
                message = (
                    "配置文件备份目标安全检查失败: "
                    f"{backup_path}"
                )
                print(message)
                self.logger.error(message)
                return False
            if not self._snapshot_path_is_safe(backup_source_path):
                message = (
                    "配置文件备份安全检查失败: "
                    f"{backup_source_path}"
                )
                print(message)
                self.logger.error(message)
                return False

            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                if not self._replace_with_staged_snapshot(
                    backup_source_path,
                    backup_path,
                ):
                    message = f"配置文件备份失败: {backup_source_path} -> {backup_path}"
                    print(message)
                    self.logger.error(message)
                    return False
            except Exception as e:
                print(f"备份配置文件时发生错误：{e}")
                self.logger.error(f"备份配置文件时发生错误：{e}")
                return False

            setattr(self, flag_name, False)
            self.logger.info(f"配置文件备份已保存到 {backup_path}")
            print(f"配置文件备份已保存到 {backup_path}")
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
        print("正在更新配置文件...")
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
                        print(f"更新配置文件: {config_file_path}")
                        self.logger.info(f"更新配置文件: {config_file_path}")
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
                        if not self.smart_remove(target_file_path):
                            print("配置文件删除失败")
                            self.logger.error("配置文件删除失败")
                            return False
                        if not self.smart_copy(
                            new_config_file_path,
                            target_file_path,
                        ):
                            print("配置文件升级失败")
                            self.logger.error("配置文件升级失败")
                            return False
                        print("配置文件升级成功")
                        self.logger.info("配置文件升级成功")
                else:
                    print("配置文件备份错误")
                    self.logger.error("配置文件备份错误")
                    return False

                if not self._apply_json_merges(json_merge_entries):
                    return False
            return True
        except Exception as e:
            print(f"配置文件升级失败: {e}")
            self.logger.error(f"配置文件升级失败: {e}")
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
