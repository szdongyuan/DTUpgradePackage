import os
import sys

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"


DEFAULT_DIR = os.path.dirname(os.path.realpath(sys.argv[0])).replace("\\", "/") + "/"

SYSTEM_DATABASE_PATH = DEFAULT_DIR + "database/system_data.db"
AUDIO_DATABASE_PATH = DEFAULT_DIR + "database/audio_data.db"
DATABASE_PATH = AUDIO_DATABASE_PATH

DB_AUDIO_COLUMNS = [
    "audio_data_id",
    "file_path",
    "product_model",
    "sample_rate",
    "record_date",
    "labels",
    "barcode",
    "stimulus_id",
]
DB_STIMULUS_COLUMNS = [
    "stimulus_id",
    "stimulus_method",
    "stimulus_type",
    "repeat_times",
    "start_freq",
    "stop_freq",
    "sample_rate",
    "total_time",
    "num_steps",
    "voltage_type",
    "voltage",
    "is_default",
    "stimulus_name",
]
DB_MODEL_COLUMNS = [
    "model_id",
    "model_name",
    "model_path",
    "config_path",
    "input_dim",
    "output_dim",
    "accuracy",
    "update_date",
    "model_description",
]
DB_USERS_COLUMNS = ["user_id", "user_name", "password", "access_level", "user_created_time", "user_updated_time"]
