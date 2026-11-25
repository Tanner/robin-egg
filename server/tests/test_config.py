"""The config file: settings read from TOML, under the command line and over the defaults. No egg, no models.

Run from server/:  python -m pytest tests/test_config.py -q
"""

from __future__ import annotations

import argparse
import logging
import os
import tomllib

import pytest

from robin_egg_server import config
from robin_egg_server.__main__ import parse_args

ROBIN = 'host = "egg.local"\nserver = "10.0.0.5"\nagent-name = "clara"\n'


@pytest.fixture
def home_file(monkeypatch, tmp_path):
    """Where the server looks for its config file when --config is not given."""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_FILE", path)
    return path


def refused(argv, capsys) -> str:
    with pytest.raises(SystemExit) as stop:
        parse_args(argv)
    assert stop.value.code == 2
    return capsys.readouterr().err


def test_the_server_starts_with_no_arguments_once_the_file_exists(home_file):
    home_file.write_text(ROBIN + 'tts = "say"\nhttp_port = 9000\nthinking-cue = false\nfollow-up-window = 4\n')
    args = parse_args([])
    assert (args.host, args.agent_name, args.tts, args.http_port, args.thinking_cue) == ("egg.local", "clara", "say", 9000, False)
    assert args.server == "http://10.0.0.5:8273"
    assert args.follow_up_window == 4.0 and isinstance(args.follow_up_window, float)
    assert args.port == 6053, "a setting the file leaves out keeps its default"


def test_the_command_line_overrides_the_file(home_file):
    home_file.write_text(ROBIN + 'tts = "say"\nthinking-cue = false\nspeaking-hours = "9-17"\n')
    args = parse_args(["other-egg.local", "--tts", "piper", "--thinking-cue"])
    assert (args.host, args.tts, args.thinking_cue) == ("other-egg.local", "piper", True)
    assert args.speaking_hours == (9, 17)


def test_a_missing_file_in_the_usual_place_is_not_an_error(home_file):
    args = parse_args(["egg.local", "--echo"])
    assert args.config_report.read is False and args.port == 6053


def test_another_file_is_read_with_config_and_must_exist(home_file, tmp_path, capsys):
    home_file.write_text('host = "home-egg.local"\n')
    other = tmp_path / "bench.toml"
    other.write_text('host = "bench-egg.local"\necho = true\n')
    assert parse_args(["--config", str(other)]).host == "bench-egg.local"
    missing = tmp_path / "nowhere.toml"
    err = refused(["--config", str(missing)], capsys)
    assert str(missing) in err and "cannot be read" in err


def test_every_option_is_a_key_and_nothing_else_is(home_file, capsys):
    p = argparse.ArgumentParser()
    p.add_argument("host", nargs="?")
    p.add_argument("--volume", type=int, default=3)
    p.add_argument("--hidden", help=argparse.SUPPRESS)
    config.add_arguments(p)
    assert set(config.settings(p)) == {"host", "volume"}
    home_file.write_text("config = \"elsewhere.toml\"\n")
    err = refused([], capsys)
    assert "'config' is not a setting" in err


def test_an_unknown_key_names_the_file_the_key_and_a_near_miss(home_file, capsys):
    home_file.write_text(ROBIN + "htp-port = 9000\n")
    err = refused([], capsys)
    assert str(home_file) in err and "'htp-port' is not a setting" in err and "'http-port'" in err


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('port = "6053"', "'port' must be a whole number such as 6053, but is text (\"6053\")"),
        ("port = 60.5", "'port' must be a whole number"),
        ("port = true", "'port' must be a whole number"),
        ('thinking-delay = "2"', "'thinking-delay' must be a number such as 2.0"),
        ('echo = "yes"', "'echo' must be true or false"),
        ("thinking-cue = 1", "'thinking-cue' must be true or false, but is a number (1)"),
        ('tts = "festival"', "'tts' must be one of \"piper\", \"say\", \"openai\""),
        ("speaking-hours = 8", "'speaking-hours' must be text in quotes such as \"8-20\""),
        ('demo = "a.wav"', "'demo' must be a list of values, each text in quotes"),
        ("[tts]\nengine = 1", "'tts' must be one of \"piper\", \"say\", \"openai\", \"elevenlabs\", but is a table"),
    ],
)
def test_a_value_of_the_wrong_type_says_what_was_expected(home_file, capsys, line, expected):
    home_file.write_text(ROBIN + line + "\n")
    err = refused([], capsys)
    assert str(home_file) in err and expected in err


def test_everything_wrong_with_the_file_is_reported_at_once(home_file, capsys):
    home_file.write_text('port = "6053"\nprot = 1\nhttp-port = 1\nhttp_port = 2\n')
    err = refused([], capsys)
    assert "'port' must be" in err and "'prot' is not a setting" in err and "'http_port' is set twice" in err


def test_a_file_that_is_not_toml_is_refused_with_the_place(home_file, capsys):
    home_file.write_text('host = "egg.local\n')
    err = refused([], capsys)
    assert str(home_file) in err and "not valid TOML" in err and "line 1" in err


def test_a_file_that_cannot_be_opened_is_refused(home_file, capsys):
    home_file.mkdir()
    err = refused([], capsys)
    assert str(home_file) in err and "cannot be read" in err


def test_the_usual_checks_apply_to_the_file_and_name_it(home_file, capsys):
    home_file.write_text(ROBIN + "quiet-volume = 20\n")
    err = refused([], capsys)
    assert "--quiet-volume 20 is outside" in err and f"'quiet-volume' in the config file {home_file}" in err
    assert str(home_file) not in refused(["--quiet-volume", "20"], capsys), "the command line's own mistake is not blamed on the file"
    home_file.write_text('host = "egg.local"\nserver = "10.0.0.5"\nagent_name = "Clara"\n')
    err = refused([], capsys)
    assert "is not an agent name" in err and str(home_file) in err


def test_api_keys_are_refused_and_pointed_at_the_environment(home_file, capsys):
    home_file.write_text(ROBIN + 'openai-api-key = "sk-not-a-real-key"\n')
    err = refused([], capsys)
    assert "Put OPENAI_API_KEY or ELEVENLABS_API_KEY in" in err and ".env" in err and "sk-not-a-real-key" not in err


def test_the_log_says_what_came_from_the_file_and_the_command_line_without_secrets(home_file, caplog):
    home_file.write_text(ROBIN + 'tts = "say"\npsk = "c2VjcmV0LWtleQ=="\n')
    home_file.chmod(0o644)
    args = parse_args(["--tts", "piper", "--port", "6054"])
    with caplog.at_level(logging.INFO):
        config.log_report(args.config_report)
    file_line, command_line, warning = caplog.messages
    assert str(home_file) in file_line and 'host="egg.local"' in file_line and "tts" not in file_line
    assert 'tts="piper" (overrides the file)' in command_line and "port=6054," in command_line
    assert "psk=(hidden)" in file_line and "c2VjcmV0" not in caplog.text
    assert "chmod 600" in warning
    caplog.clear()
    home_file.chmod(0o600)
    with caplog.at_level(logging.INFO):
        config.log_report(parse_args(["--psk", "c2VjcmV0LWtleQ=="]).config_report)
    assert "chmod" not in caplog.text and "c2VjcmV0" not in caplog.text and "psk=(hidden) (overrides the file)" in caplog.text


def test_the_log_says_when_there_is_no_file(home_file, caplog):
    with caplog.at_level(logging.INFO):
        config.log_report(parse_args(["egg.local", "--echo"]).config_report)
    assert f"No config file at {home_file}" in caplog.messages[0]
    assert 'host="egg.local", echo=true' in caplog.messages[1]


def test_print_config_writes_a_file_that_reads_back_as_the_same_settings(home_file, capsys):
    home_file.write_text(ROBIN + 'tts = "say"\npsk = "c2VjcmV0LWtleQ=="\n')
    with pytest.raises(SystemExit) as stop:
        parse_args(["--print-config", "--port", "6054", "--no-closing-cue"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "c2VjcmV0" not in out and "# psk is set, but is not printed" in out
    printed = tomllib.loads(out)
    assert printed["host"] == "egg.local" and printed["tts"] == "say" and printed["port"] == 6054 and printed["closing-cue"] is False
    assert printed["speaking-hours"] == "8-20", "values are printed as they are written, not as the server reads them"
    assert "config" not in printed and "print-config" not in printed
    home_file.write_text(out)
    again = parse_args([])
    assert (again.host, again.tts, again.port, again.closing_cue) == ("egg.local", "say", 6054, False)


def test_print_config_with_nothing_set_is_a_starting_file(home_file, capsys):
    with pytest.raises(SystemExit) as stop:
        parse_args(["--print-config"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "# host =" in out and "port = 6053" in out
    home_file.write_text(out)
    assert parse_args(["egg.local", "--echo"]).port == 6053


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """Where the server looks for its .env file when --env-file is not given."""
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", path)
    return path


def test_the_env_file_puts_the_api_keys_in_the_environment(env_file, monkeypatch):
    env_file.write_text('# Robin Egg secrets\n\nexport OPENAI_API_KEY="sk-from-the-file"\nELEVENLABS_API_KEY=el-from-the-file\n')
    report = parse_args(["egg.local", "--echo"]).config_report
    assert "OPENAI_API_KEY" not in os.environ, "parsing alone leaves the environment alone"
    monkeypatch.setattr(os, "environ", dict(os.environ))
    config.apply_env(report)
    assert os.environ["OPENAI_API_KEY"] == "sk-from-the-file"
    assert os.environ["ELEVENLABS_API_KEY"] == "el-from-the-file"


def test_the_environment_wins_over_the_env_file(env_file, monkeypatch, caplog):
    env_file.write_text("OPENAI_API_KEY=sk-from-the-file\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell")
    report = parse_args(["egg.local", "--echo"]).config_report
    config.apply_env(report)
    assert os.environ["OPENAI_API_KEY"] == "sk-from-the-shell"
    with caplog.at_level(logging.INFO):
        config.log_report(report)
    assert "not used: OPENAI_API_KEY" in caplog.text and "sk-from" not in caplog.text


def test_the_eggs_key_comes_from_the_env_file_under_the_command_line(env_file, home_file):
    env_file.write_text("ROBIN_EGG_PSK='a2V5LWZyb20tZW52'\n")
    assert parse_args(["egg.local", "--echo"]).psk == "a2V5LWZyb20tZW52"
    assert parse_args(["egg.local", "--echo", "--psk", "a2V5LWNsaQ=="]).psk == "a2V5LWNsaQ=="
    home_file.write_text('psk = "a2V5LWZyb20tdG9tbA=="\n')
    args = parse_args(["egg.local", "--echo"])
    assert args.psk == "a2V5LWZyb20tZW52", "the secret's own file wins over the config file"
    assert "(overridden by ROBIN_EGG_PSK)" in args.config_report.from_file["psk"]


def test_the_log_names_the_secrets_but_never_their_values(env_file, caplog):
    env_file.write_text("OPENAI_API_KEY=sk-from-the-file\nROBIN_EGG_PSK=a2V5LWZyb20tZW52\n")
    env_file.chmod(0o644)
    with caplog.at_level(logging.INFO):
        config.log_report(parse_args(["egg.local", "--echo"]).config_report)
    assert f"Read secrets from the .env file {env_file}: OPENAI_API_KEY, ROBIN_EGG_PSK" in caplog.text
    assert "comes from ROBIN_EGG_PSK" in caplog.text and "chmod 600" in caplog.text
    assert "sk-from" not in caplog.text and "a2V5" not in caplog.text


def test_a_missing_env_file_in_the_usual_place_is_not_an_error(env_file):
    report = parse_args(["egg.local", "--echo"]).config_report
    assert report.env_read is False and report.env == {}


def test_another_env_file_is_read_with_env_file_and_must_exist(env_file, tmp_path, capsys):
    other = tmp_path / "bench.env"
    other.write_text("ROBIN_EGG_PSK=YmVuY2g=\n")
    assert parse_args(["egg.local", "--echo", "--env-file", str(other)]).psk == "YmVuY2g="
    err = refused(["egg.local", "--echo", "--env-file", str(tmp_path / "missing.env")], capsys)
    assert "missing.env cannot be read" in err


def test_everything_wrong_with_the_env_file_is_reported_at_once_without_values(env_file, capsys):
    env_file.write_text("OPENAI_KEY=sk-secret-one\njust some words\nROBIN_EGG_PSK=\nOPENAI_API_KEY=a\nOPENAI_API_KEY=b\n")
    err = refused(["egg.local", "--echo"], capsys)
    assert "line 1: OPENAI_KEY is not a secret the server reads. Did you mean OPENAI_API_KEY?" in err
    assert "line 2 is not NAME=value" in err and "line 3: ROBIN_EGG_PSK is empty" in err and "line 5: OPENAI_API_KEY is set twice" in err
    assert "sk-secret-one" not in err


def test_env_file_is_not_a_setting(home_file, capsys):
    home_file.write_text('env-file = "/tmp/x.env"\n')
    assert "'env-file' is not a setting" in refused([], capsys)


def test_an_option_is_its_flag_name_even_when_argparse_stores_it_elsewhere(home_file, tmp_path, caplog):
    home_file.write_text(ROBIN + f'save-wake-clips = "{tmp_path}/wakes"\nsave-audio-requests = "{tmp_path}/captures"\nsave-audio-requests-hours = 6\n')
    args = parse_args([])
    assert args.wake_clips == tmp_path / "wakes"
    assert args.save_audio == f"{tmp_path}/captures" and args.save_audio_hours == 6.0
    with caplog.at_level(logging.INFO):
        config.log_report(args.config_report)
    assert "save-wake-clips=" in caplog.text and "save-audio-requests-hours=6.0" in caplog.text
    assert "wake-clips=" not in caplog.text.replace("save-wake-clips=", "")


def test_an_options_other_spelling_is_a_key_too_but_not_both(home_file, capsys):
    home_file.write_text(ROBIN + 'save-audio = "/tmp/captures"\n')
    assert parse_args([]).save_audio == "/tmp/captures"
    home_file.write_text(ROBIN + 'save-audio = "/tmp/a"\nsave-audio-requests = "/tmp/b"\n')
    assert "set twice" in refused([], capsys)


def test_argparses_own_names_are_not_keys(home_file, capsys):
    home_file.write_text(ROBIN + 'wake-clips = "/tmp/wakes"\n')
    assert "Did you mean 'save-wake-clips'?" in refused([], capsys)


def test_print_config_uses_the_flag_names(home_file, capsys):
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--save-wake-clips=/tmp/wakes", "--print-config"])
    printed = capsys.readouterr().out
    assert 'save-wake-clips = "/tmp/wakes"' in printed and "save-audio-requests-hours = 24.0" in printed
    assert "\nwake-clips" not in printed and "# wake-clips" not in printed
