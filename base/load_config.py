import json
import os
import re
import tempfile
import yaml

from datetime import datetime
from re import _parser as re_parser

from consts import error_code
from consts.running_consts import DEFAULT_DIR, SEQUENCE_CONFIG_REGISTRY_PATH, SN_REGEX_RULES_JSON_PATH
from base.log_manager import LogManager


def load_config(config_path, module_name=None):
    """
    Load configuration from a YAML file. Optionally, retrieve specific module configuration.

    Args:
    - module_name : string
        The name of the module whose configuration you want to retrieve.
        If None, the entire configuration is loaded.
    Returns:
    - result : dictionary
        The configuration dictionary that stores specific module configurations
        or entire configurations.
    """

    result = {}
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f.read())
        if module_name:
            for module_config in config:
                if module_config.get("module_name") == module_name:
                    result = module_config.get("module_config", {})
        else:
            result = config
    return result


class LoadUiConfig(object):

    @staticmethod
    def _get_default_sn_regex_rule():
        return {
            "id": "default-match-all",
            "name": "默认全匹配",
            "pattern": "^.+$",
            "is_default": True,
        }

    @staticmethod
    def _get_sn_regex_rules_logger():
        return LogManager.set_log_handler("core")

    @staticmethod
    def _log_sn_regex_rules(level, message):
        try:
            logger = LoadUiConfig._get_sn_regex_rules_logger()
            log_method = getattr(logger, level, None)
            if callable(log_method):
                log_method(message)
        except Exception:
            pass

    @staticmethod
    def build_default_sn_regex_rules_payload():
        default_rule = LoadUiConfig._get_default_sn_regex_rule()
        return {
            "version": 1,
            "selected_rule_id": default_rule["id"],
            "rules": [default_rule],
        }

    @staticmethod
    def _resolve_sn_regex_rules_json_path(json_file_path=None):
        if json_file_path:
            return os.fspath(json_file_path)
        return SN_REGEX_RULES_JSON_PATH

    @staticmethod
    def _normalize_sn_regex_rules_payload(config_data):
        default_payload = LoadUiConfig.build_default_sn_regex_rules_payload()
        default_rule_id = default_payload["selected_rule_id"]
        builtin_default_rule = LoadUiConfig._get_default_sn_regex_rule()
        fallback_required = False
        normalization_reasons = []

        if not isinstance(config_data, dict):
            return default_payload, True, ["rules payload is not a dict"]

        rules = config_data.get("rules")
        if not isinstance(rules, list) or not rules:
            return default_payload, True, ["rules list is missing or empty"]

        normalized_rules = []
        seen_rule_ids = set()
        for rule in rules:
            if not isinstance(rule, dict):
                fallback_required = True
                normalization_reasons.append("ignored non-dict rule entry")
                continue

            rule_id = rule.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                fallback_required = True
                normalization_reasons.append("ignored rule with invalid id")
                continue
            if rule_id == default_rule_id:
                if rule_id in seen_rule_ids:
                    fallback_required = True
                    normalization_reasons.append("ignored duplicate default rule entry")
                    continue

                if (
                    rule.get("name") != builtin_default_rule["name"]
                    or rule.get("pattern") != builtin_default_rule["pattern"]
                    or bool(rule.get("is_default", False)) != builtin_default_rule["is_default"]
                ):
                    fallback_required = True
                    normalization_reasons.append("restored built-in default rule definition")

                normalized_rules.append(dict(builtin_default_rule))
                seen_rule_ids.add(default_rule_id)
                continue

            rule_name = rule.get("name")
            pattern = rule.get("pattern")
            if not isinstance(rule_name, str) or not rule_name:
                fallback_required = True
                normalization_reasons.append(f"ignored rule '{rule_id}' with invalid name")
                continue
            if not isinstance(pattern, str) or not pattern:
                fallback_required = True
                normalization_reasons.append(f"ignored rule '{rule_id}' with invalid pattern")
                continue
            if not LoadUiConfig.can_compile_sn_regex_pattern(pattern):
                fallback_required = True
                normalization_reasons.append(f"ignored rule '{rule_id}' with invalid regex")
                continue
            if LoadUiConfig.is_pure_literal_sn_regex_pattern(pattern):
                fallback_required = True
                normalization_reasons.append(f"ignored rule '{rule_id}' with literal-only regex")
                continue
            if rule_id in seen_rule_ids:
                fallback_required = True
                normalization_reasons.append(f"ignored duplicate rule id '{rule_id}'")
                continue

            if bool(rule.get("is_default", False)):
                fallback_required = True
                normalization_reasons.append(f"reset non-default rule '{rule_id}' is_default flag")

            normalized_rules.append(
                {
                    "id": rule_id,
                    "name": rule_name,
                    "pattern": pattern,
                    "is_default": False,
                }
            )
            seen_rule_ids.add(rule_id)

        if not normalized_rules:
            return default_payload, True, normalization_reasons + ["no valid rules remained after normalization"]

        default_rule = None
        for rule in normalized_rules:
            if rule["id"] == default_rule_id:
                default_rule = rule
                break

        if default_rule is None:
            default_rule = LoadUiConfig._get_default_sn_regex_rule()
            normalized_rules.insert(0, default_rule)
            seen_rule_ids.add(default_rule_id)
            fallback_required = True
            normalization_reasons.append("reinserted missing built-in default rule")
        if not default_rule["is_default"]:
            default_rule["is_default"] = True
            fallback_required = True
            normalization_reasons.append("restored default rule is_default flag")

        selected_rule_id = config_data.get("selected_rule_id")
        if not isinstance(selected_rule_id, str) or selected_rule_id not in seen_rule_ids:
            selected_rule_id = default_rule_id
            fallback_required = True
            normalization_reasons.append("reset selected rule to built-in default")

        version = config_data.get("version", default_payload["version"])
        if not isinstance(version, int):
            version = default_payload["version"]
            fallback_required = True
            normalization_reasons.append("reset invalid rules payload version")

        normalized_payload = {
            "version": version,
            "selected_rule_id": selected_rule_id,
            "rules": normalized_rules,
        }
        if normalized_payload != config_data:
            fallback_required = True
            normalization_reasons.append("persisted normalized SN regex rules payload")
        return normalized_payload, fallback_required, normalization_reasons

    @staticmethod
    def _is_valid_sn_regex_rules_payload(config_data):
        normalized_payload, should_persist, _ = LoadUiConfig._normalize_sn_regex_rules_payload(config_data)
        if should_persist:
            return False
        for rule in normalized_payload["rules"]:
            if not LoadUiConfig.can_compile_sn_regex_pattern(rule["pattern"]):
                return False
            if LoadUiConfig.is_pure_literal_sn_regex_pattern(rule["pattern"]):
                return False
        return True

    @staticmethod
    def load_sn_regex_rules_from_json(json_file_path=None):
        json_file_path = LoadUiConfig._resolve_sn_regex_rules_json_path(json_file_path)
        default_payload = LoadUiConfig.build_default_sn_regex_rules_payload()
        try:
            if not os.path.exists(json_file_path):
                if not LoadUiConfig.save_sn_regex_rules_to_json(default_payload, json_file_path):
                    LoadUiConfig._log_sn_regex_rules(
                        "error",
                        f"Failed to persist recovered default SN regex rules to {json_file_path}.",
                    )
                return default_payload
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                config_data = json.load(json_file)
        except Exception as exc:
            LoadUiConfig._log_sn_regex_rules(
                "warning",
                f"Failed to load SN regex rules from {json_file_path}; recovered default payload. {exc}",
            )
            if not LoadUiConfig.save_sn_regex_rules_to_json(default_payload, json_file_path):
                LoadUiConfig._log_sn_regex_rules(
                    "error",
                    f"Failed to persist recovered default SN regex rules to {json_file_path}.",
                )
            return default_payload

        normalized_payload, should_persist, normalization_reasons = LoadUiConfig._normalize_sn_regex_rules_payload(
            config_data
        )
        if should_persist:
            LoadUiConfig._log_sn_regex_rules(
                "warning",
                f"Normalized SN regex rules from {json_file_path}: {'; '.join(normalization_reasons)}",
            )
            if not LoadUiConfig.save_sn_regex_rules_to_json(normalized_payload, json_file_path):
                LoadUiConfig._log_sn_regex_rules(
                    "error",
                    f"Failed to persist normalized SN regex rules to {json_file_path}.",
                )
        return normalized_payload

    @staticmethod
    def save_sn_regex_rules_to_json(config_data, json_file_path=None):
        json_file_path = LoadUiConfig._resolve_sn_regex_rules_json_path(json_file_path)
        if not LoadUiConfig._is_valid_sn_regex_rules_payload(config_data):
            return False
        return LoadUiConfig._write_json_atomically(config_data, json_file_path)

    @staticmethod
    def _write_json_atomically(config_data, json_file_path):
        target_path = os.path.abspath(json_file_path)
        parent_dir = os.path.dirname(target_path)
        temp_file_fd = None
        temp_file_path = None
        try:
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            temp_file_fd, temp_file_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(target_path)}.",
                suffix=".tmp",
                dir=parent_dir,
            )
            with os.fdopen(temp_file_fd, "w", encoding="utf-8") as json_file:
                temp_file_fd = None
                json.dump(config_data, json_file, indent=6, ensure_ascii=False)
                json_file.flush()
                os.fsync(json_file.fileno())
            os.replace(temp_file_path, target_path)
            return True
        except Exception:
            return False
        finally:
            if temp_file_fd is not None:
                try:
                    os.close(temp_file_fd)
                except OSError:
                    pass
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass

    @staticmethod
    def get_selected_sn_regex_rule(config_data):
        normalized_payload, _, _ = LoadUiConfig._normalize_sn_regex_rules_payload(config_data)
        selected_rule_id = normalized_payload["selected_rule_id"]
        for rule in normalized_payload["rules"]:
            if rule["id"] == selected_rule_id:
                return dict(rule)
        return LoadUiConfig._get_default_sn_regex_rule()

    @staticmethod
    def can_compile_sn_regex_pattern(pattern):
        if not isinstance(pattern, str) or not pattern:
            return False
        try:
            re.compile(pattern)
            return True
        except re.error:
            return False

    @staticmethod
    def _is_literal_only_sn_regex_subpattern(parsed_subpattern):
        allowed_anchor_tokens = {
            re_parser.AT_BEGINNING,
            re_parser.AT_BEGINNING_LINE,
            re_parser.AT_BEGINNING_STRING,
            re_parser.AT_END,
            re_parser.AT_END_LINE,
            re_parser.AT_END_STRING,
        }
        for token_type, token_value in parsed_subpattern:
            if token_type == re_parser.LITERAL:
                continue
            if token_type == re_parser.AT and token_value in allowed_anchor_tokens:
                continue
            if token_type == re_parser.SUBPATTERN and LoadUiConfig._is_literal_only_sn_regex_subpattern(
                token_value[-1]
            ):
                continue
            return False
        return True

    @staticmethod
    def is_pure_literal_sn_regex_pattern(pattern):
        if not isinstance(pattern, str) or not pattern:
            return False
        try:
            parsed_pattern = re_parser.parse(pattern, 0)
        except re.error:
            return False
        return LoadUiConfig._is_literal_only_sn_regex_subpattern(parsed_pattern)

    @staticmethod
    def load_sequence_config_from_json(json_file_path):
        """
        Loads analysis sequence configuration data using the **new list-based format**.

        The new JSON layout is a list, whose first element is a dict with a single
        sequence key (e.g. "seq1").  Each sequence contains an "acq" section and an
        "analysis_list" section that keeps the previous flat configuration.  This
        function extracts and returns that inner ``analysis_list`` so that the rest
        of the code can keep working with the same dict structure as before.
        """
        if not json_file_path or not isinstance(json_file_path, (str, bytes, os.PathLike)):
            return error_code.INVALID_DATA_LOADING, "Invalid json file path."
        if not os.path.exists(json_file_path):
            return error_code.INVALID_DATA_LOADING, "This json file does not exist."
        try:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                analysis_config = json.load(json_file)
                return error_code.OK, analysis_config
        except Exception as e:
            err_msg = "Failed to load analysis sequence data from json.%s" % (str(e)[:50])
            return error_code.INVALID_DATA_LOADING, err_msg

    @staticmethod
    def load_data_from_json(json_file_path):
        """
        Loads data from a specified JSON file and returns it with an error code.

        This method attempts to load JSON data from the provided file path. It first checks
        if the file exists, and if not, returns an error code with a descriptive message.

        Args:
            json_file_path (str): The path to the JSON file to be loaded.

        Returns:
            tuple: A tuple containing:
                - error_code (int): error_code.OK on success,
                  error_code.INVALID_DATA_LOADING on failure
                - data (dict/list) or error_message (str): Parsed JSON data on success,
                  error description on failure
        """
        if not os.path.exists(json_file_path):
            return error_code.INVALID_DATA_LOADING, "This json file does not exist."
        try:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                analysis_config = json.load(json_file)
            return error_code.OK, analysis_config
        except Exception as e:
            err_msg = f"Failed to load analysis sequence data from json. {str(e)[:50]}"
            return error_code.INVALID_DATA_LOADING, err_msg

    @staticmethod
    def save_sequence_config_to_json(config_data, json_file_path):
        """Save ``config_data`` (the inner analysis_list dict) back to json file using the new format."""
        os.makedirs(os.path.dirname(json_file_path), exist_ok=True)
        try:
            with open(json_file_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=6, ensure_ascii=False)
            return True
        except Exception as e:
            return False

    @staticmethod
    def _load_sequence_config_registry(registry_path: str = None) -> dict:
        """
        Load the sequence config registry JSON.

        Returns an empty dict if:
        - file doesn't exist
        - file content is invalid / not a dict
        """
        registry_path = registry_path or SEQUENCE_CONFIG_REGISTRY_PATH
        try:
            if not os.path.exists(registry_path):
                return {}
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            # Treat as empty registry on any parse/read error
            return {}

    @staticmethod
    def _save_sequence_config_registry(registry: dict, registry_path: str = None) -> bool:
        """Write registry JSON to disk (creates parent dir)."""
        registry_path = registry_path or SEQUENCE_CONFIG_REGISTRY_PATH
        try:
            os.makedirs(os.path.dirname(registry_path), exist_ok=True)
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(registry or {}, f, indent=6, ensure_ascii=False)
            return True
        except Exception:
            return False

    @staticmethod
    def append_sequence_config_registry_entry(file_path: str, registry_path: str = None) -> bool:
        """
        Append/update one entry to registry using filename (without extension) as key,
        and full file path as value.
        """
        if not file_path:
            return False
        registry_path = registry_path or SEQUENCE_CONFIG_REGISTRY_PATH
        try:
            key = os.path.splitext(os.path.basename(file_path))[0]
            if not key:
                return False
            registry = LoadUiConfig._load_sequence_config_registry(registry_path)
            registry[key] = file_path
            return LoadUiConfig._save_sequence_config_registry(registry, registry_path)
        except Exception:
            return False

    @staticmethod
    def ensure_sequence_config_registry_field(field_key: str, field_value: str, registry_path: str = None) -> bool:
        """
        Ensure registry contains the given field_key.
        If missing, write field_key -> field_value.
        """
        if not field_key:
            return False
        registry_path = registry_path or SEQUENCE_CONFIG_REGISTRY_PATH
        try:
            registry = LoadUiConfig._load_sequence_config_registry(registry_path)
            if field_key in registry:
                return True
            registry[field_key] = field_value
            return LoadUiConfig._save_sequence_config_registry(registry, registry_path)
        except Exception:
            return False

    @staticmethod
    def update_using_config_path(using_config_path, registry_path: str = None) -> bool:
        """
        Update the using config path in the registry.
        """
        if not using_config_path:
            return False
        registry_path = registry_path or SEQUENCE_CONFIG_REGISTRY_PATH
        registry = LoadUiConfig._load_sequence_config_registry()
        registry["using_config_path"] = using_config_path
        return LoadUiConfig._save_sequence_config_registry(registry)

    @staticmethod
    def load_last_recorded_info(logger):
        """
        Load the recorded number from a text file.

        This method reads a recorded number and the last recorded date from a specified text file.
        If the file exists and the last recorded date matches the current date, it returns the recorded number;
        otherwise, it returns None.

        Returns:
            int or None: The recorded number if the file exists and the date matches; otherwise, None.
        """
        file_path = DEFAULT_DIR + "ui/ui_config/recorded_number.json"
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
                return data
        except Exception as e:
            logger.error(f"Failed to read the info of recorded number: {str(e)[:50]}")
            return None

    @staticmethod
    def load_recorded_num_from_json(logger):
        """
        Load the recorded number from a text file.

        This method reads a recorded number and the last recorded date from a specified text file.
        If the file exists and the last recorded date matches the current date, it returns the recorded number;
        otherwise, it returns None.

        Returns:
            int or None: The recorded number if the file exists and the date matches; otherwise, None.
        """
        result = LoadUiConfig().load_last_recorded_info(logger)
        if result:
            last_datetime = result.get("datetime")
            recorded_count = result.get("current_recorded_count")
            scanner_barcode = result.get("scanner_barcode")
            if last_datetime == datetime.now().strftime("%Y-%m-%d"):
                return recorded_count, scanner_barcode

        return None, None

    @staticmethod
    def get_rec_and_play_dict_base_sequence_dict(data_struct, total_time=None):
        """
        Generate dictionaries containing stimulus signal data and recording parameters.

        This function creates two dictionaries: one for the stimulus signal data and its related information,
        and another for the recording parameters. These dictionaries are used for subsequent signal processing and analysis.

        Args:
        - sample_rate (int): The sampling rate, indicating the number of samples collected per second.
        - total_time (int): The total recording time, indicates the duration of the recording.

        Returns:
        - stimulus_dict (dict): Dictionary containing the stimulus signal data and related information.
        - recorded_dict (dict): Dictionary containing the recording parameters.
        """
        # Define the prolongation time to calculate the extended frame count
        prolong = 0.5
        stimulus_dict = dict()
        if data_struct.stimulus_data is not None and len(data_struct.stimulus_data) > 0:
            stimulus_dict = {
                "data": data_struct.stimulus_data,
                "amplitude": data_struct.stimulus_info["amplitude"],
                "sr": data_struct.sample_rate,
            }
            num_frames = len(data_struct.stimulus_data) + int(prolong * data_struct.sample_rate)
            prolong_frames = int(prolong * data_struct.sample_rate)
        else:
            num_frames = int(total_time * data_struct.sample_rate)
            prolong_frames = 0
        recorded_dict = {
            "channels": 1,
            "sr": data_struct.sample_rate,
            "num_frames": num_frames,
            "prolong_frames": prolong_frames,
        }
        return stimulus_dict, recorded_dict

    @staticmethod
    def write_tcp_config(ip, port, logger):
        file_path = DEFAULT_DIR + "ui/ui_config/tcp_config.txt"

        try:
            with open(file_path, "w") as f:
                f.write(f"ip = {ip}\n")
                f.write(f"port = {port}\n")
            logger.info(f"write_tcp_config_success: {file_path}")
        except Exception as e:
            logger.error(f"write_tcp_config_error: {e}")

    @staticmethod
    def get_tcp_config():
        file_path = DEFAULT_DIR + "ui/ui_config/tcp_config.txt"
        with open(file_path, "r") as f:
            config_data = f.readlines()
            ip = config_data[0].split("=")[1].strip()
            port_text = config_data[1].split("=")[1].strip()
            port = int(port_text)
            return ip, port


class ConfigManager(object):
    """负责读写分析窗口各项配置的通用管理器，迁移自 ui.analysis_config_window"""

    def __init__(self, config_file):
        self.config_file = config_file
        self.default_logger = LogManager.set_log_handler("core")
        self.config = {}

    def save_config(self, type, config_data):
        if type in self.config:
            self.config[type].update(config_data)
        else:
            self.config[type] = config_data
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4)
                self.default_logger.info(f"The config info for {type} analysis has been saved to {self.config_file}.")
                return True
        except Exception as e:
            self.default_logger.error(f"The config info for {type} analysis save failed. {e}")
            return False

    def save_default_config(self, type, config_data):
        default_config_file = DEFAULT_DIR + "ui/ui_config/analysis_default_config.json"
        default_config = {}
        try:
            with open(default_config_file, "r", encoding="utf-8") as f:
                default_config = json.load(f)
                if type in default_config:
                    default_config[type].update(config_data)
                else:
                    default_config[type] = config_data
            with open(default_config_file, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=4)
                self.default_logger.info(
                    f"The config info for {type} analysis has been saved to {default_config_file}."
                )
                return True
        except Exception as e:
            self.default_logger.error(f"Failed to load the default config file. {e}")
            return False

    def load_config(self):
        try:
            if self.config:
                return self.config
            with open(self.config_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            if isinstance(raw_data, list):
                self.config = LoadUiConfig._extract_analysis_list(raw_data)
            else:
                self.config = raw_data
            return self.config
        except Exception as e:
            self.default_logger.error(f"Failed to load the default or temp config file. {e}")
            return {}
