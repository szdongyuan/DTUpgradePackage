import yaml
import sqlite3
from pprint import pprint

from consts.program_config import DEFAULT_DIR, DEFAULT_TARGET_DIR, DATABASE_PATH, BACKUP_DIR

class Readyml(object):
    def __init__(self, path):
        self.path = path
        self.yaml_file = None
        self.yaml_data = None

    def load_yaml(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            self.yaml_file = f.read()
        self.yaml_data = yaml.load(self.yaml_file, Loader=yaml.FullLoader)
        pprint(self.yaml_data)

    def read_database(self):
        print(DATABASE_PATH)
        connect = sqlite3.connect(DATABASE_PATH)
        cursor = connect.cursor()
        a = cursor.execute("SELECT stimulus_id FROM stimulus_signal_table;").fetchall()
        b = {item[0] for item in a}
        print(b, "\n\n")



if __name__ == '__main__':
    yml = Readyml(DEFAULT_DIR + 'resources/version_1.1.yml')
    # yml.load_yaml()
    # print(yml.yaml_data)
    yml.read_database()
