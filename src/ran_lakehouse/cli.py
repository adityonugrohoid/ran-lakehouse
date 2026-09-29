"""The `ranlake` command line."""

import argparse
from collections.abc import Sequence

from ran_lakehouse import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `ranlake` command.

    Returns:
        The parser, with one subcommand required.
    """
    parser = argparse.ArgumentParser(
        prog="ranlake",
        description="Mini telecom lakehouse for a synthetic LTE and GSM operator.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("version", help="print the package version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `ranlake` command.

    Args:
        argv: Command-line arguments without the program name, or None to
            read them from sys.argv.

    Returns:
        The process exit code.

    Raises:
        AssertionError: If a parsed subcommand has no handler.
    """
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
