from pathlib import Path
import sys


SRC_PATH = Path(__file__).resolve().parent / 'src'
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from things_manager.app import *  # noqa: F401,F403


if __name__ == '__main__':
    run_dev_server()
