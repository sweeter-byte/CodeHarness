"""CodeHarness CLI entry point."""

from codeharness.app import CodeHarness
from interfaces.cli import CLI


def main() -> None:
    harness = CodeHarness.from_env()
    try:
        harness.start()
        CLI(harness).run()
    finally:
        harness.close()


if __name__ == "__main__":
    main()
