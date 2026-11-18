"""Settings from a TOML file, so the server starts without a long command line.

The accepted keys are read off the command line's own argparse definitions:
every option is a key under the same name, with the same type and choices.
The command line overrides the file, and the file overrides the defaults.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("robin")

DEFAULT_CONFIG_FILE = Path("~/.robin-egg/config.toml").expanduser()
# Options that say how to read the settings are not settings themselves.
NOT_SETTINGS = {"config", "print_config"}
SECRET_WORDS = ("psk", "key", "token", "secret", "password")
SWITCHES = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)
STORES = (argparse._StoreAction, *SWITCHES)


@dataclass
class ConfigReport:
    """Where the settings of this start came from, for the log."""

    path: Path
    read: bool = False
    from_file: dict[str, str] = field(default_factory=dict)
    from_command_line: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", metavar="FILE", help=f"Read settings from this TOML file instead of {tilde(DEFAULT_CONFIG_FILE)}. Every option here is a key in it, without the leading dashes; the command line overrides the file")
    p.add_argument("--print-config", action="store_true", help="Print the settings this start would use as TOML, then exit. With no config file yet, this is a starting one")


def settings(p: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    """The options that can be set in the file, by key."""
    return {
        a.dest: a
        for a in p._actions
        if isinstance(a, STORES) and a.dest not in NOT_SETTINGS and a.help is not argparse.SUPPRESS
    }


def is_secret(dest: str) -> bool:
    return any(word in dest.split("_") for word in SECRET_WORDS)


def name(dest: str) -> str:
    return dest.replace("_", "-")


def tilde(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + "/") else str(path)


def shown(dest: str, value: Any) -> str:
    return f"{name(dest)}=(hidden)" if is_secret(dest) else f"{name(dest)}={toml_value(value)}"


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def described(value: Any) -> str:
    """What the file holds instead, in words."""
    if isinstance(value, bool):
        return toml_value(value)
    if isinstance(value, dict):
        return "a table"
    kind = "text" if isinstance(value, str) else "a list" if isinstance(value, list) else "a number" if isinstance(value, (int, float)) else type(value).__name__
    return f"{kind} ({toml_value(value)})"


def expected(action: argparse.Action) -> str:
    """What a key's value has to be, in words."""
    if isinstance(action, SWITCHES):
        return "true or false"
    if action.choices is not None:
        one = "one of " + ", ".join(toml_value(c) for c in action.choices)
    elif action.type is int:
        one = "a whole number" + (f" such as {action.default}" if isinstance(action.default, int) else "")
    elif action.type is float:
        one = "a number" + (f" such as {action.default}" if isinstance(action.default, (int, float)) else "")
    else:
        one = "text in quotes" + (f" such as {toml_value(action.default)}" if isinstance(action.default, str) and not is_secret(action.dest) else "")
    return f"a list of values, each {one}" if action.nargs in ("+", "*") else one


def convert_one(action: argparse.Action, value: Any) -> Any:
    """The value as the command line would have produced it, or ValueError."""
    if action.type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError
    elif action.type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        value = float(value)
    elif action.type is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        # A custom type such as --digest-max parses text, as the command line would give it.
        value = convert_one(action, str(value))
    elif not isinstance(value, str):
        raise ValueError
    elif action.type is not None:
        try:
            value = action.type(value)
        except (TypeError, argparse.ArgumentTypeError) as err:
            raise ValueError from err
    if action.choices is not None and value not in action.choices:
        raise ValueError
    return value


def convert(action: argparse.Action, value: Any) -> Any:
    if isinstance(action, SWITCHES):
        if not isinstance(value, bool):
            raise ValueError
        return value
    if action.nargs in ("+", "*"):
        if not isinstance(value, list) or (action.nargs == "+" and not value):
            raise ValueError
        return [convert_one(action, v) for v in value]
    return convert_one(action, value)


def read_file(path: Path, known: dict[str, argparse.Action]) -> dict[str, Any]:
    """The file's settings by option, or ValueError listing everything wrong with it."""
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except OSError as err:
        raise ValueError(f"The config file {path} cannot be read: {err.strerror or err}") from err
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise ValueError(f"The config file {path} is not valid TOML: {err}") from err
    values: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in raw.items():
        dest = key.replace("-", "_")
        if dest not in known:
            if "api_key" in dest.lower():
                problems.append(f"{key!r} is not a setting: API keys are not kept in this file. The hosted voice reads OPENAI_API_KEY from the environment")
                continue
            close = difflib.get_close_matches(dest, known, n=1)
            hint = f" Did you mean {name(close[0])!r}?" if close else ""
            problems.append(f"{key!r} is not a setting.{hint} The settings are the options listed by --help, without the leading dashes")
        elif dest in values:
            problems.append(f"{key!r} is set twice, once with hyphens and once with underscores")
        else:
            try:
                values[dest] = convert(known[dest], value)
            except ValueError:
                problems.append(f"{key!r} must be {expected(known[dest])}, but is {described(value)}")
    if problems:
        raise ValueError(f"The config file {path} has {'a setting' if len(problems) == 1 else 'settings'} the server cannot use:\n  " + "\n  ".join(problems))
    return values


def given_on_command_line(p: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """Only the options the command line itself sets, found by parsing with every default taken away."""
    defaults = [(a, a.default) for a in p._actions]
    for action, _ in defaults:
        action.default = argparse.SUPPRESS
    try:
        return p.parse_args(argv)
    finally:
        for action, default in defaults:
            action.default = default


def name_the_file_in_errors(p: argparse.ArgumentParser, path: Path, from_file: set[str]) -> None:
    """Make the parser's later complaints about a value say so when the value came from the file."""
    flags = {flag: a.dest for a in p._actions for flag in a.option_strings}
    plain = p.error

    def error(message: str):
        words = [w.split("=")[0] for w in message.split() if w.startswith("--")]
        dest = flags.get(words[0].rstrip(".,:;")) if words else None
        if dest in from_file:
            message = f"{message} (this is {name(dest)!r} in the config file {path})"
        plain(message)

    p.error = error


def parse(p: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line over the config file over the defaults.

    The namespace gets a `config_report` saying where each setting came from.
    """
    given = given_on_command_line(p, argv)
    known = settings(p)
    explicit = getattr(given, "config", None)
    path = Path(explicit).expanduser() if explicit else DEFAULT_CONFIG_FILE
    report = ConfigReport(path)
    values: dict[str, Any] = {}
    if explicit or path.exists():
        try:
            values = read_file(path, known)
        except ValueError as err:
            p.error(str(err))
        report.read = True
        with_secrets = [name(d) for d in values if is_secret(d)]
        if with_secrets and stat.S_IMODE(path.stat().st_mode) & 0o077:
            report.warnings.append(f"The config file {path} holds {' and '.join(with_secrets)} and can be read by other accounts on this Mac; chmod 600 keeps it private")
    p.set_defaults(**values)
    args = p.parse_args(argv)
    report.from_file = {d: shown(d, v) for d, v in values.items() if not hasattr(given, d)}
    report.from_command_line = {d: shown(d, getattr(given, d)) + (" (overrides the file)" if d in values else "") for d in known if hasattr(given, d)}
    args.config_report = report
    if args.print_config:
        sys.stdout.write(as_toml(known, args))
        sys.exit(0)
    name_the_file_in_errors(p, path, set(report.from_file))
    return args


def as_toml(known: dict[str, argparse.Action], args: argparse.Namespace) -> str:
    """The effective settings as a config file, each under its help text."""
    lines = ["# Settings for the Robin Egg Server. The command line overrides this file.", ""]
    for dest, action in known.items():
        value = getattr(args, dest, None)
        lines.append("# " + " ".join(str(action.help or "").split()))
        if value is None:
            lines.append(f"# {name(dest)} =")
        elif is_secret(dest):
            lines.append(f"# {name(dest)} is set, but is not printed")
        else:
            lines.append(f"{name(dest)} = {toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def log_report(report: ConfigReport) -> None:
    if report.read:
        LOG.info("Read settings from the config file %s: %s", report.path, ", ".join(report.from_file.values()) or "none that the command line does not override")
    else:
        LOG.info("No config file at %s, so settings come from the command line and the defaults", report.path)
    if report.from_command_line:
        LOG.info("Settings from the command line: %s", ", ".join(report.from_command_line.values()))
    for warning in report.warnings:
        LOG.warning(warning)
