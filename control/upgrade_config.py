import sys
import os
import shutil
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
                self.logger.error(f"源路径不存在: {src_path}")
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
                    self.logger.info(f"目录内容复制成功: {src_path} -> {dst_path}")
                    print(f"目录内容复制成功: {src_path} -> {dst_path}")
                else:
                    # 目标是文件路径或不存在，复制整个目录
                    if os.path.exists(dst_path):
                        if os.path.isfile(dst_path):
                            os.remove(dst_path)
                        else:
                            shutil.rmtree(dst_path)
                    shutil.copytree(src_path, dst_path)
                    self.logger.info(f"目录复制成功: {src_path} -> {dst_path}")
                    print(f"目录复制成功: {src_path} -> {dst_path}")

            # 处理源路径是文件的情况
            elif os.path.isfile(src_path):
                # 确保目标目录存在
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir)

                shutil.copy2(src_path, dst_path)
                self.logger.info(f"文件复制成功: {src_path} -> {dst_path}")
                print(f"文件复制成功: {src_path} -> {dst_path}")

            return True

        except Exception as e:
            self.logger.error(f"复制失败 {src_path} -> {dst_path}: {e}")
            print(f"复制失败 {src_path} -> {dst_path}: {e}")
            return False

    def _cleanup_snapshot_artifact(self, path):
        if not path or not os.path.exists(path):
            return True

        try:
            if self.smart_remove(path):
                return True
        except Exception as e:
            self.logger.error(f"快照临时路径清理失败 {path}: {e}")
            return False

        self.logger.error(f"快照临时路径清理失败: {path}")
        return False

    def _replace_with_staged_snapshot(self, src_path, dst_path):
        destination_path = os.path.normpath(dst_path)
        parent_path = os.path.dirname(destination_path) or os.curdir
        destination_name = os.path.basename(destination_path)
        staging_path = None
        old_path = None
        preserve_old_path = False

        try:
            os.makedirs(parent_path, exist_ok=True)
            staging_path = tempfile.mkdtemp(
                prefix=f".{destination_name}.staging-",
                dir=parent_path,
            )
            if not self.smart_copy(src_path, staging_path):
                self.logger.error(
                    f"快照暂存复制失败: {src_path} -> {staging_path}"
                )
                return False

            if os.path.exists(destination_path):
                old_path = tempfile.mkdtemp(
                    prefix=f".{destination_name}.old-",
                    dir=parent_path,
                )
                os.rmdir(old_path)
                os.replace(destination_path, old_path)

            try:
                os.replace(staging_path, destination_path)
                staging_path = None
            except Exception as swap_error:
                if old_path is not None:
                    try:
                        os.replace(old_path, destination_path)
                        old_path = None
                    except Exception as rollback_error:
                        preserve_old_path = True
                        self.logger.error(
                            "快照替换回滚失败，旧目标保留在 "
                            f"{old_path}: {rollback_error}"
                        )
                self.logger.error(
                    f"快照替换失败 {staging_path} -> {destination_path}: "
                    f"{swap_error}"
                )
                return False

            if old_path is not None:
                if not self._cleanup_snapshot_artifact(old_path):
                    preserve_old_path = True
                    return False
                old_path = None

            return True
        except Exception as e:
            self.logger.error(
                f"快照替换失败 {src_path} -> {destination_path}: {e}"
            )
            return False
        finally:
            self._cleanup_snapshot_artifact(staging_path)
            if not preserve_old_path:
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
                self.logger.warning(f"路径不存在: {path}")
                return True  # 不存在也算"删除成功"

            if os.path.isfile(path):
                # 删除文件
                os.remove(path)
                self.logger.info(f"文件删除成功: {path}")
                print(f"文件删除成功: {path}")
            elif os.path.isdir(path):
                # 删除目录及其内容
                shutil.rmtree(path)
                self.logger.info(f"目录删除成功: {path}")
                print(f"目录删除成功: {path}")
            else:
                self.logger.warning(f"未知的路径类型: {path}")
                return False

            return True

        except PermissionError as e:
            self.logger.error(f"权限错误，无法删除 {path}: {e}")
            print(f"权限错误，无法删除 {path}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"删除失败 {path}: {e}")
            print(f"删除失败 {path}: {e}")
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
            file_path = DEFAULT_TARGET_DIR + file_path
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

    def _ordinary_config_manifest_path_key(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.strip():
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

    def _report_json_merge(self, level, message):
        print(message)
        getattr(self.logger, level)(message)

    def _apply_json_merges(self, entries, config_entries):
        config_target_keys = {
            target_key
            for target_key in (
                self._ordinary_config_manifest_path_key(config_entry)
                for config_entry in config_entries
            )
            if target_key is not None
        }
        for entry in entries:
            target_key = self._json_merge_manifest_path_key(entry["target"])
            if target_key is not None and target_key in config_target_keys:
                self._report_json_merge(
                    "error",
                    "json_merge error: target_conflicts_with_config",
                )
                return False

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
                if self.backup_config_file(config_file_source_path):
                    for config_file_path in config_file_source_path:
                        print(f"更新配置文件: {config_file_path}")
                        self.logger.info(f"更新配置文件: {config_file_path}")
                        file_name = config_file_path.split("/")[-1]
                        new_config_file_path = CONFIG_DIR + file_name
                        target_file_path = DEFAULT_TARGET_DIR + config_file_path
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
                if not self._apply_json_merges(
                    json_merge_entries,
                    config_file_source_path,
                ):
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
                self.logger.error(f"{display_name}恢复失败: {e}")

            if copy_succeeded:
                print(f"{display_name}已恢复")
                self.logger.info(f"{display_name}已恢复")
            else:
                restore_succeeded = False
                message = f"{display_name}恢复失败: {backup_path} -> {target_path}"
                print(message)
                self.logger.error(message)

        if not restore_succeeded:
            print("配置文件恢复失败，保留备份文件")
            return False

        print("配置文件已恢复")
        return self.delete_backup_file()

    def delete_backup_file(self):
        self.logger.info("正在删除备份文件...")
        print("正在删除备份文件...")
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
                self.logger.error(f"{display_name}删除失败: {e}")

            if removed:
                print(f"{display_name}已删除")
                self.logger.info(f"{display_name}已删除")
            else:
                delete_succeeded = False
                message = f"{display_name}删除失败，保留备份: {backup_path}"
                print(message)
                self.logger.error(message)

        return delete_succeeded


if __name__ == "__main__":
    from log_model.log_manager import LogManager

    log_manager = LogManager()
    config_manager = ConfigManager(log_manager, "0.25.06.01")
    config_manager.update_config_file()
