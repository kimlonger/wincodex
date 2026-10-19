import argparse
import sys

from .config import build_arg_parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from .gui import run_gui

        return run_gui(args)

    from .cli import run_cli

    return run_cli(args)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
