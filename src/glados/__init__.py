"""GLaDOS - Grounded Logic and Desktop Orchestration System."""

__version__ = "0.1.0"


def main() -> int:
    """`glados` 명령의 시작점."""
    from glados.cli import main as cli_main

    return cli_main()
