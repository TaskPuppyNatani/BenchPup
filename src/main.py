from pathlib import Path

from cli import TerminalApp

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    TerminalApp(ROOT / "data" / "benchmark.db").run()


if __name__ == "__main__":
    main()
