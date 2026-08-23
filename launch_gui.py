"""PySide6 GUI and frozen internal CLI entrypoint."""

from __future__ import annotations

import json
import os
import sys


def _internal_command_name(cli_module: object, arguments: list[str]) -> str:
    parser = cli_module.build_parser()
    command_names: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            command_names.update(str(name) for name in choices)
    return next((value for value in arguments if value in command_names), "unknown")


def _configure_internal_stdout() -> None:
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="strict", newline="\n")
    except (AttributeError, OSError, ValueError):
        return


def _internal_cli_main() -> int:
    from codex_sync import cli
    from codex_sync.progress import set_progress_sink

    _configure_internal_stdout()
    sys.argv.remove("--internal-cli")
    os.environ["CODEX_SYNC_JSON_LINES"] = "1"
    secret_count = int(os.environ.pop("CODEX_SYNC_INTERNAL_SECRET_COUNT", "0") or "0")
    secrets: list[str] = []
    if secret_count:
        try:
            payload = json.loads(sys.stdin.readline())
        except (json.JSONDecodeError, OSError) as exc:
            print(
                json.dumps(
                    {"ok": False, "error": f"Internal secret input is invalid: {exc}"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 2
        values = payload.get("secret_inputs") if isinstance(payload, dict) else None
        if not isinstance(values, list) or len(values) != secret_count:
            print(
                json.dumps(
                    {"ok": False, "error": "Internal secret input count does not match"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 2
        secrets = [str(value) for value in values]

    secret_iterator = iter(secrets)
    previous_getpass = cli.getpass.getpass

    def next_secret(_prompt: str = "") -> str:
        try:
            return next(secret_iterator)
        except StopIteration as exc:
            raise RuntimeError("Internal CLI did not receive a required secret") from exc

    def emit(payload: dict[str, object]) -> None:
        print(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )

    set_progress_sink(emit)
    cli.getpass.getpass = next_secret
    command = _internal_command_name(cli, sys.argv[1:])
    emit({"event": "started", "task": command})
    try:
        return cli.main()
    finally:
        cli.getpass.getpass = previous_getpass
        set_progress_sink(None)
        for index in range(len(secrets)):
            secrets[index] = ""


def main() -> int:
    if "--internal-cli" in sys.argv:
        return _internal_cli_main()

    from codex_sync.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
