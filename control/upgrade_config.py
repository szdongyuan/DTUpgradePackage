import sys
import os
import shutil

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
        for file_path in config_file_source_path:
            file_path = DEFAULT_TARGET_DIR + file_path
            backup_source_path = None
            backup_path = None
            if self.ai_model_config_flag or self.scannre_barcode_config_flag or self.ui_pic_flag or self.ui_config_flag:
                if self.is_parent_path_os(AI_MODEL_CONFIG_PATH, file_path) and self.ai_model_config_flag:
                    backup_path = BACKUP_AI_MODEL_CONFIG_PATH
                    backup_source_path = AI_MODEL_CONFIG_PATH
                    self.ai_model_config_flag = False
                elif self.is_parent_path_os(UI_CONFIG_PATH, file_path) and self.ui_config_flag:
                    backup_path = BACKUP_UI_CONFIG_PATH
                    backup_source_path = UI_CONFIG_PATH
                    self.ui_config_flag = False
                elif self.is_parent_path_os(UI_PIC_PATH, file_path) and self.ui_pic_flag:
                    backup_path = BACKUP_UI_PIC_PATH
                    backup_source_path = UI_PIC_PATH
                    self.ui_pic_flag = False
                elif (
                    self.is_parent_path_os(SCANNER_BARCODE_CONFIG_PATH, file_path) and self.scannre_barcode_config_flag
                ):
                    backup_path = BACKUP_SCANNER_BARCODE_CONFIG_PATH
                    backup_source_path = SCANNER_BARCODE_CONFIG_PATH
                    self.scannre_barcode_config_flag = False

                if not os.path.exists(BACKUP_DIR):
                    os.makedirs(BACKUP_DIR)
                try:
                    self.smart_copy(backup_source_path, backup_path)
                    self.logger.info(f"配置文件备份已保存到 {backup_path}")
                    print(f"配置文件备份已保存到 {backup_path}")
                except Exception as e:
                    print(f"备份配置文件时发生错误：{e}")
                    self.logger.error(f"备份配置文件时发生错误：{e}")
                    return False
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
                        if self.smart_remove(target_file_path):
                            if self.smart_copy(new_config_file_path, target_file_path):
                                print("配置文件升级成功")
                                self.logger.info("配置文件升级成功")
                            else:
                                print("配置文件升级失败")
                                self.logger.error("配置文件备份错误")
                else:
                    print("配置文件备份错误")
                    self.logger.error("配置文件备份错误")
                    return False
            return True
        except Exception as e:
            print(f"配置文件升级失败: {e}")
            self.logger.error(f"配置文件升级失败: {e}")
            return False

    def restore_config(self):
        try:
            if self.ui_pic_flag is False:
                self.smart_copy(BACKUP_UI_PIC_PATH, UI_PIC_PATH)
                print("ui图片配置文件已恢复")
                self.logger.info("ui图片配置文件已恢复")
            if self.ai_model_config_flag is False:
                self.smart_copy(BACKUP_AI_MODEL_CONFIG_PATH, AI_MODEL_CONFIG_PATH)
                print("ai模型配置文件已恢复")
                self.logger.info("ai模型配置文件已恢复")
            if self.scannre_barcode_config_flag is False:
                self.smart_copy(BACKUP_SCANNER_BARCODE_CONFIG_PATH, SCANNER_BARCODE_CONFIG_PATH)
                print("条码扫描配置文件已恢复")
                self.logger.info("条码扫描配置文件已恢复")
            if self.ui_config_flag is False:
                self.smart_copy(BACKUP_UI_CONFIG_PATH, UI_CONFIG_PATH)
                print("UI配置文件已恢复")
                self.logger.info("UI配置文件已恢复")
            print("配置文件已恢复")
            self.delete_backup_file()
        except Exception as e:
            print(f"配置文件恢复失败: {e}")

    def delete_backup_file(self):
        self.logger.info("正在删除备份文件...")
        print("正在删除备份文件...")
        try:
            if self.ui_pic_flag is False:
                self.smart_remove(BACKUP_UI_PIC_PATH)
                print("ui图片配置备份文件已删除")
                self.logger.info("ui图片配置备份文件已删除")
            if self.ai_model_config_flag is False:
                self.smart_remove(BACKUP_AI_MODEL_CONFIG_PATH, AI_MODEL_CONFIG_PATH)
                print("ai模型配置备份文件已删除")
                self.logger.info("ai模型配置备份文件已删除")
            if self.scannre_barcode_config_flag is False:
                self.smart_remove(BACKUP_SCANNER_BARCODE_CONFIG_PATH, SCANNER_BARCODE_CONFIG_PATH)
                print("条码扫描配置备份文件已删除")
                self.logger.info("条码扫描配置备份文件已删除")
            if self.ui_config_flag is False:
                self.smart_remove(BACKUP_UI_CONFIG_PATH, UI_CONFIG_PATH)
                print("UI配置备份文件已删除")
                self.logger.info("UI配置备份文件已删除")
        except Exception as e:
            print(f"配置文件删除失败: {e}")


if __name__ == "__main__":
    from log_model.log_manager import LogManager

    log_manager = LogManager()
    config_manager = ConfigManager(log_manager, "0.25.06.01")
    config_manager.update_config_file()
