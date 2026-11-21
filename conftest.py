"""pytest 入口：把仓库根目录加入 sys.path，使 tests 能 import meta。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
