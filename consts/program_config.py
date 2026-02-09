import os
import sys

# DEFAULT_TARGET_DIR = os.path.split(os.path.realpath(__file__))[0].replace("\\", "/") + "/../../SpeakerAnomalyDetection/"
# DEFAULT_DIR = os.path.split(os.path.realpath(__file__))[0].replace("\\", "/") + "/../"
DEFAULT_TARGET_DIR = os.path.dirname(os.path.realpath(sys.argv[0])).replace("\\", "/") + "/../"
DEFAULT_DIR = os.path.dirname(os.path.realpath(sys.argv[0])).replace("\\", "/") + "/"
DATABASE_PATH = DEFAULT_TARGET_DIR + "database/audio_data.db"
# DATABASE_PATH = DEFAULT_DIR + "resources/audio_data.db"
BACKUP_DIR = DEFAULT_DIR + "resources/backup/"
CONFIG_DIR = DEFAULT_DIR + "resources/config/"

DEFAULT_LOG_FORMATTER = "[%(asctime)s][%(name)s] - [%(levelname)s] - [%(message)s] [%(filename)s:%(lineno)d]"
LOG_DIR = DEFAULT_DIR + "log_model/log/"

UI_PIC_PATH = DEFAULT_TARGET_DIR + "ui/ui_pic/"
UI_CONFIG_PATH = DEFAULT_TARGET_DIR + "ui/ui_config/"
AI_MODEL_CONFIG_PATH = DEFAULT_TARGET_DIR + "configs/aimodel_config/"
SCANNER_BARCODE_CONFIG_PATH = DEFAULT_TARGET_DIR + "configs/scanner_barcode_config/"

BACKUP_UI_PIC_PATH = BACKUP_DIR + "ui_pic/"
BACKUP_UI_CONFIG_PATH = BACKUP_DIR + "ui_config/"
BACKUP_AI_MODEL_CONFIG_PATH = BACKUP_DIR + "ai_model_config/"
BACKUP_SCANNER_BARCODE_CONFIG_PATH = BACKUP_DIR + "scanner_barcode_config/"

ALL_VERSIONS = ["0.12", "0.25.06.01", "0.25.07.01", "0.25.09.01", "0.25.12.01", "0.26.02.01"]
TARGET_VERSION = "0.26.02.01"

KB = 1 << 10
MB = 1 << 20
GB = 1 << 30

DEFAULT_LOG = {
    "log_name": LOG_DIR + "main.log",
    "max_size": 2 * MB,
    "backup_count": 9,
    "log_format": DEFAULT_LOG_FORMATTER,
}

AI_LOG = {
    "log_name": LOG_DIR + "ai.log",
    "max_size": 10 * KB,
    "backup_count": 9,
    "log_format": DEFAULT_LOG_FORMATTER,
}
DEBUG_LOG = {
    "log_name": LOG_DIR + "debug.log",
    "max_size": 1 * MB,
    "backup_count": 0,
    "log_format": DEFAULT_LOG_FORMATTER,
}

TEST_LOG = {
    "log_name": LOG_DIR + "test.log",
    "max_size": 100 * KB,
    "backup_count": 0,
    "log_format": DEFAULT_LOG_FORMATTER,
}

LOG_MAPPING = {
    "core": DEFAULT_LOG,
    "train": AI_LOG,
    "evaluate": AI_LOG,
    "predict": AI_LOG,
    "debug": DEBUG_LOG,
    "test": TEST_LOG,
    "db_core": DEFAULT_LOG,
    "soundcard_core": DEFAULT_LOG,
}
