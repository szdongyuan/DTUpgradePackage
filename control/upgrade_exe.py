import codecs
import locale
import os
import shutil
import subprocess
import sys

from consts.program_config import DEFAULT_DIR
from load_yml.load_yml import Readyml


class ExeUpgradeManager(object):
    def __init__(self, logger, upgrade_version_list, base_dir=None):
        self.logger = logger
        self.upgrade_version_list = upgrade_version_list or []
        self.base_dir = os.path.realpath(base_dir or DEFAULT_DIR)

    def run(self):
        self._log_info("正在处理升级可执行程序...")
        for upgrade_version in self.upgrade_version_list:
            target_version_path = os.path.join(self.base_dir, "resources", f"version_{upgrade_version}.yml")
            data = Readyml.load_yaml(target_version_path) or {}
            if "exe" not in data or data["exe"] is None:
                continue
            exe_entries = data["exe"]
            if exe_entries == []:
                continue
            if not isinstance(exe_entries, list):
                self._log_error("exe 配置必须是列表")
                return False
            if not self._run_entries(exe_entries, upgrade_version):
                return False
        return True

    def _run_entries(self, exe_entries, upgrade_version):
        for index, entry in enumerate(exe_entries, start=1):
            config, error, required = self._normalize_entry(entry)
            if error:
                message = f"版本 {upgrade_version} 第 {index} 个 exe 配置错误: {error}"
                if not self._handle_failure(message, required):
                    return False
                continue
            if not self._run_one(config):
                return False
        return True

    def _normalize_entry(self, entry):
        if isinstance(entry, str):
            raw_config = {"path": entry}
        elif isinstance(entry, dict):
            raw_config = entry
        else:
            return None, "exe 配置项必须是字符串或字典", True

        required = raw_config.get("required", True)
        if not isinstance(required, bool):
            required = True

        raw_path = raw_config.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None, "exe 配置 path 必须是非空字符串", required

        raw_name = raw_config.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        else:
            name = raw_path

        args = raw_config.get("args")
        if args is None:
            args = []
        elif not isinstance(args, list):
            return None, "exe 配置 args 必须是列表", required
        else:
            normalized_args = []
            for arg in args:
                if isinstance(arg, bool) or not isinstance(arg, (str, int, float)):
                    return None, "exe 配置 args 只能包含字符串、整数或浮点数", required
                normalized_args.append(str(arg))
            args = normalized_args

        timeout = raw_config.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                return None, "exe 配置 timeout 必须是大于 0 的数字", required

        if "output_encoding" not in raw_config:
            output_encoding = None
        else:
            raw_output_encoding = raw_config["output_encoding"]
            if not isinstance(raw_output_encoding, str) or not raw_output_encoding.strip():
                return None, "exe 配置 output_encoding 必须是非空字符串", required
            try:
                output_encoding = codecs.lookup(raw_output_encoding.strip()).name
            except LookupError:
                return None, f"exe 配置 output_encoding 无效或不受支持: {raw_output_encoding}", required

        copy_to_parent = raw_config.get("copy_to_parent", False)
        if not isinstance(copy_to_parent, bool):
            copy_to_parent = False

        cleanup_after_run = raw_config.get("cleanup_after_run", False)
        if not isinstance(cleanup_after_run, bool):
            cleanup_after_run = False

        resolved_path, error = self._resolve_path(raw_path)
        if error:
            return None, error, required
        if not os.path.isfile(resolved_path):
            return None, f"可执行程序路径不存在或不是文件: {resolved_path}", required

        raw_cwd = raw_config.get("cwd")
        if raw_cwd is None:
            cwd = self._get_parent_dir() if copy_to_parent else os.path.dirname(resolved_path)
        else:
            if not isinstance(raw_cwd, str) or not raw_cwd.strip():
                return None, "exe 配置 cwd 必须是非空字符串", required
            cwd, error = self._resolve_path(raw_cwd)
            if error:
                return None, error, required
        if not os.path.isdir(cwd):
            return None, f"工作目录不存在或不是目录: {cwd}", required

        return {
            "name": name,
            "path": resolved_path,
            "args": args,
            "cwd": cwd,
            "timeout": timeout,
            "output_encoding": output_encoding,
            "required": required,
            "copy_to_parent": copy_to_parent,
            "cleanup_after_run": cleanup_after_run,
        }, None, required

    def _resolve_path(self, value):
        if os.path.isabs(value):
            return os.path.realpath(value), None

        resolved = os.path.realpath(os.path.join(self.base_dir, value))
        if not self._is_inside_base(resolved):
            return None, f"相对路径不能超出升级包目录: {value}"
        return resolved, None

    def _is_inside_base(self, path):
        try:
            base_dir = os.path.normcase(self.base_dir)
            child_path = os.path.normcase(path)
            return os.path.commonpath([base_dir, child_path]) == base_dir
        except (ValueError, OSError):
            return False

    def _get_parent_dir(self):
        return os.path.realpath(os.path.join(self.base_dir, os.pardir))

    def _copy_to_parent(self, source_path):
        parent_dir = self._get_parent_dir()
        target_path = os.path.realpath(os.path.join(parent_dir, os.path.basename(source_path)))
        try:
            parent_norm = os.path.normcase(parent_dir)
            target_norm = os.path.normcase(target_path)
            if os.path.commonpath([parent_norm, target_norm]) != parent_norm:
                return None, f"临时可执行程序目标路径不合法: {target_path}"
        except (ValueError, OSError):
            return None, f"临时可执行程序目标路径不合法: {target_path}"

        if os.path.exists(target_path):
            return None, f"临时可执行程序目标已存在: {target_path}"

        try:
            shutil.copy2(source_path, target_path)
        except Exception as e:
            return None, f"复制可执行程序失败: {source_path} -> {target_path}, 错误: {e}"

        self._log_info(f"临时复制可执行程序: {source_path} -> {target_path}")
        return target_path, None

    def _cleanup_temp_file(self, temp_path):
        if not temp_path:
            return True
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                self._log_info(f"临时可执行程序已删除: {temp_path}")
            return True
        except Exception as e:
            self._log_error(f"删除临时可执行程序失败: {temp_path}, 错误: {e}")
            return False

    def _run_one(self, config):
        run_path = config["path"]
        temp_path = None
        if config["copy_to_parent"]:
            temp_path, error = self._copy_to_parent(config["path"])
            if error:
                return self._handle_failure(error, config["required"])
            run_path = temp_path

        command = [run_path] + config["args"]
        start_message = (
            f"启动可执行程序: {config['name']}, 路径: {run_path}, "
            f"参数: {config['args']}, 工作目录: {config['cwd']}"
        )
        self._log_info(start_message)
        result_success = False
        try:
            result = subprocess.run(
                command,
                cwd=config["cwd"],
                timeout=config["timeout"],
                capture_output=True,
                text=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as e:
            message = f"可执行程序执行超时: {config['name']}, 超时时间: {config['timeout']} 秒"
            result_success = self._handle_failure(message, config["required"])
            self._log_captured_output(
                config["name"],
                e.stdout,
                e.stderr,
                config["output_encoding"],
            )
        except Exception as e:
            message = f"可执行程序启动失败: {config['name']}, 路径: {run_path}, 错误: {e}"
            result_success = self._handle_failure(message, config["required"])
        else:
            self._log_info(f"可执行程序执行结束: {config['name']}, 退出码: {result.returncode}")
            self._log_captured_output(
                config["name"],
                result.stdout,
                result.stderr,
                config["output_encoding"],
            )
            if result.returncode != 0:
                message = f"可执行程序返回非零退出码: {config['name']}, 退出码: {result.returncode}"
                result_success = self._handle_failure(message, config["required"])
            else:
                result_success = True
        finally:
            if config["cleanup_after_run"] and temp_path:
                cleanup_success = self._cleanup_temp_file(temp_path)
                if result_success and not cleanup_success:
                    return False
        return result_success

    def _log_captured_output(self, name, stdout, stderr, output_encoding=None):
        stdout_text = self._normalize_output(stdout, output_encoding).rstrip()
        stderr_text = self._normalize_output(stderr, output_encoding).rstrip()
        if stdout_text:
            self._log_info(f"可执行程序标准输出: {name}\n{stdout_text}")
        if stderr_text:
            self._log_warning(f"可执行程序错误输出: {name}\n{stderr_text}")

    def _normalize_output(self, output, output_encoding=None):
        if output is None:
            return ""
        if isinstance(output, bytes):
            if output_encoding is not None:
                return output.decode(output_encoding, errors="replace")

            try:
                return output.decode("utf-8")
            except UnicodeDecodeError:
                pass

            local_encodings = []
            seen_encodings = set()

            def add_encoding(encoding):
                if not isinstance(encoding, str) or not encoding.strip():
                    return
                encoding = encoding.strip()
                normalized_encoding = encoding.casefold()
                if normalized_encoding in seen_encodings:
                    return
                if normalized_encoding.replace("_", "-") in {
                    "utf8",
                    "utf-8",
                }:
                    return
                seen_encodings.add(normalized_encoding)
                local_encodings.append(encoding)

            try:
                preferred_encoding = locale.getpreferredencoding(False)
            except Exception:
                preferred_encoding = None

            if os.name == "nt":
                add_encoding(preferred_encoding)
                add_encoding(getattr(sys.stdout, "encoding", None))
                add_encoding(getattr(sys.stderr, "encoding", None))
                add_encoding("mbcs")
                add_encoding("gbk")
            else:
                add_encoding(preferred_encoding)
                add_encoding(getattr(sys.stdout, "encoding", None))
                add_encoding(getattr(sys.stderr, "encoding", None))

            for encoding in local_encodings:
                try:
                    return output.decode(encoding)
                except (LookupError, UnicodeDecodeError):
                    continue

            for encoding in local_encodings:
                try:
                    return output.decode(encoding, errors="replace")
                except LookupError:
                    continue
            return output.decode("utf-8", errors="replace")
        return str(output)

    def _handle_failure(self, message, required):
        if required:
            self._log_error(message)
            return False
        self._log_warning(message)
        return True

    def _log_info(self, message):
        self._report_best_effort("info", message)

    def _log_warning(self, message):
        self._report_best_effort("warning", message)

    def _log_error(self, message):
        self._report_best_effort("error", message)

    def _report_best_effort(self, level, message):
        try:
            print(message)
        except Exception:
            pass
        try:
            if self.logger:
                getattr(self.logger, level)(message)
        except Exception:
            pass
