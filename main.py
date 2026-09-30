"""CodeHarness CLI entry point."""

from codeharness.app import CodeHarness
from interfaces.cli import CLI


def main() -> None:
    harness = CodeHarness.from_env()
    cli = CLI(harness)
    try:
        harness.start()
        cli.run()
    finally:
        harness.close()


if __name__ == "__main__":
    main()
