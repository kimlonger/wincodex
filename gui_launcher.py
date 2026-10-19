"""打包成 exe 后的入口：双击直接进图形界面。

命令行用户仍可用 `python -m chatcode --gui`，两者走的是同一套界面代码。
"""

import sys
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

from chatcode.config import build_arg_parser
from chatcode.gui import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui(build_arg_parser().parse_args()))
