import copy
import importlib.util
from pathlib import Path
import stat
import subprocess
from unittest.mock import Mock

import pytest
import yaml


spec = importlib.util.spec_from_file_location(
    "_mihomo_share_proxy_tests", Path(__file__).parents[1] / "tools/mihomo_share_proxy.py")
share = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share)


def config():
    return {
        "mixed-port": 7890,
        "secret": "controller-secret",
        "proxies": [{"name": "AG-SOCKS5-7894", "type": "socks5",
                     "server": "example.invalid", "port": 5001,
                     "username": "upstream", "password": "upstream-secret",
                     "dialer-proxy": "Antigravity"}],
        "proxy-groups": [{"name": "Antigravity", "type": "select", "proxies": ["DIRECT"]}],
        "listeners": [{"name": "internal", "type": "mixed", "port": 7894,
                       "listen": "172.17.0.1", "proxy": "AG-SOCKS5-7894"}],
        "rules": ["MATCH,DIRECT"],
    }


def test_addition_preserves_config_and_generates_separate_credentials():
    original = config()
    expected = copy.deepcopy(original)
    updated, users, changed = share.listener_config(original, 17895, "proxyuser", "AG-SOCKS5-7894")
    password = users[0]["password"]
    assert changed and password != "upstream-secret" and len(password) == 32
    assert original == expected
    listener = updated["listeners"].pop()
    assert updated == expected
    assert listener == {
        "name": "share-auth-17895", "type": "mixed", "listen": "0.0.0.0", "port": 17895,
        "proxy": "AG-SOCKS5-7894", "udp": False,
        "users": [{"username": "proxyuser", "password": password}],
    }


def test_existing_listener_keeps_password():
    original, users, _ = share.listener_config(config(), 17895, "proxyuser", "AG-SOCKS5-7894")
    updated, second_users, changed = share.listener_config(
        original, 17895, "proxyuser", "AG-SOCKS5-7894")
    assert not changed and second_users == users and updated == original


def test_multiple_custom_users_are_used_on_first_install():
    custom = [{"username": "alice", "password": "alice-password"},
              {"username": "bob", "password": "bob-password"}]
    updated, users, changed = share.listener_config(config(), 17895, "proxyuser", "AG-SOCKS5-7894", custom)
    assert changed and users == custom
    assert updated["listeners"][-1]["users"] == custom
    assert users is not custom
    same, preserved, needs_apply = share.listener_config(updated, 17895, "proxyuser", "AG-SOCKS5-7894")
    assert not needs_apply and same == updated and preserved == custom


def test_add_and_rotate_only_named_accounts():
    original, users, _ = share.listener_config(config(), 17895, "proxyuser", "AG-SOCKS5-7894")
    saved = copy.deepcopy(original)
    updated, new_users, changed = share.listener_config(original, 17895, "proxyuser", "AG-SOCKS5-7894", [
        {"username": "alice", "password": "alice-password"},
        {"username": "bob", "password": "bob-password"},
    ])
    assert changed and new_users[0] == users[0] and original == saved
    rotated, rotated_users, changed = share.listener_config(updated, 17895, "proxyuser", "AG-SOCKS5-7894", [
        {"username": "alice", "password": "new-password"}])
    assert changed and len(rotated_users) == 3
    assert rotated_users[0] == new_users[0] and rotated_users[2] == new_users[2]
    assert new_users[1]["password"] == "alice-password"
    assert rotated_users[1]["password"] == "new-password"
    repeated, _, changed = share.listener_config(rotated, 17895, "proxyuser", "AG-SOCKS5-7894", [
        {"username": "alice", "password": "new-password"}])
    assert not changed and repeated == rotated


@pytest.mark.parametrize("users", [[], [{"username": "alice", "password": ""}],
    [{"username": "alice", "password": "test"}, {"username": "alice", "password": "other"}],
    [{"username": "has:colon", "password": "test"}],
    [{"username": "alice", "password": "x" * 256}],
    [{"username": "alice", "password": "newline\n"}],
    [{"username": "alice", "password": 1234}]])
def test_invalid_user_updates_stop_without_changing_config(users):
    original = config()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError):
        share.listener_config(original, 17895, "proxyuser", "AG-SOCKS5-7894", users)
    assert original == before


def test_custom_passwords_are_prompted_twice(monkeypatch):
    monkeypatch.setattr(share.sys.stdin, "isatty", lambda: True)
    prompt = Mock(side_effect=["alice-password", "alice-password", "bob-password", "bob-password"])
    monkeypatch.setattr(share.getpass, "getpass", prompt)
    assert share.prompt_users(["alice", "bob"]) == [
        {"username": "alice", "password": "alice-password"},
        {"username": "bob", "password": "bob-password"}]
    assert prompt.call_count == 4


@pytest.mark.parametrize("answers", [["short"], ["alice-password", "different-password"]])
def test_short_or_mismatched_passwords_are_rejected(monkeypatch, answers):
    monkeypatch.setattr(share.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(share.getpass, "getpass", Mock(side_effect=answers))
    with pytest.raises(ValueError):
        share.prompt_users(["alice"])


def test_noninteractive_input_does_not_read_passwords(monkeypatch):
    monkeypatch.setattr(share.sys.stdin, "isatty", lambda: False)
    prompt = Mock()
    monkeypatch.setattr(share.getpass, "getpass", prompt)
    with pytest.raises(ValueError, match="interactive SSH"):
        share.prompt_users(["alice"])
    prompt.assert_not_called()


def test_duplicate_names_are_rejected_before_prompting(monkeypatch):
    monkeypatch.setattr(share.sys.stdin, "isatty", lambda: True)
    prompt = Mock()
    monkeypatch.setattr(share.getpass, "getpass", prompt)
    with pytest.raises(ValueError, match="only once"):
        share.prompt_users(["alice", "alice"])
    prompt.assert_not_called()


def test_credential_sheet_uses_domain_and_lists_all_users():
    text = share.credential_text(17895, [{"username": "alice", "password": "one"},
                                       {"username": "bob", "password": "two"}])
    assert "Address: proxy.example.com\nPort: 17895\n" in text
    assert "Username: alice\nPassword: one\n" in text
    assert "Username: bob\nPassword: two\n" in text


@pytest.mark.parametrize("port", [7890, 7894])
def test_port_conflicts_are_not_overwritten(port):
    original = config()
    expected = copy.deepcopy(original)
    with pytest.raises(ValueError, match="another"):
        share.listener_config(original, port, "proxyuser", "AG-SOCKS5-7894")
    assert original == expected


def test_missing_outbound_is_not_replaced_with_direct():
    with pytest.raises(ValueError, match="was not found"):
        share.listener_config(config(), 17895, "proxyuser", "missing")


@pytest.mark.parametrize("key,value", [("udp", True), ("proxy", "DIRECT"),
                                     ("users", []), ("listen", "127.0.0.1")])
def test_existing_listener_conflicts_stop(key, value):
    original, _, _ = share.listener_config(config(), 17895, "proxyuser", "AG-SOCKS5-7894")
    original["listeners"][-1][key] = value
    with pytest.raises(ValueError):
        share.listener_config(original, 17895, "proxyuser", "AG-SOCKS5-7894")


@pytest.fixture
def setup_files(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    source = "# Preserve this comment in the backup.\n" + yaml.safe_dump(config(), sort_keys=False)
    path.write_text(source, encoding="utf-8")
    updated, users, _ = share.listener_config(config(), 17895, "proxyuser", "AG-SOCKS5-7894")
    controller = Mock()
    monkeypatch.setattr(share.subprocess, "check_output", Mock(return_value="123\n"))
    monkeypatch.setattr(share.subprocess, "run", Mock())
    monkeypatch.setattr(share, "verify_auth", Mock())
    return path, source, updated, users, controller, tmp_path / "credentials.txt"


def apply(setup_files):
    path, _, updated, users, controller, info = setup_files
    share.apply_config(path, updated, controller, {"Antigravity": "DIRECT"},
                       "mihomo", 17895, users, info)


def test_apply_validates_and_saves_backup_and_credentials(setup_files):
    path, source, updated, users, controller, info = setup_files
    password = users[0]["password"]
    apply(setup_files)
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == updated
    backups = list(path.parent.glob("config.yaml.before-share-auth.*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == source
    assert password in info.read_text(encoding="utf-8")
    assert share.subprocess.run.call_args.args[0][0] == "/proc/123/exe"
    assert share.subprocess.run.call_args.kwargs == {"check": True, "timeout": 45}
    assert controller.call.call_args_list[0].args == ("/configs?force=true", {"path": str(path)})
    assert controller.call.call_args_list[1].args == ("/proxies/Antigravity", {"name": "DIRECT"})
    share.verify_auth.assert_called_once_with(17895, "proxyuser", password)
    if share.os.name == "posix":
        assert stat.S_IMODE(info.stat().st_mode) == 0o600


def test_invalid_candidate_does_not_modify_config(setup_files):
    path, source, _, _, controller, info = setup_files
    share.subprocess.run.side_effect = subprocess.CalledProcessError(1, "mihomo")
    with pytest.raises(subprocess.CalledProcessError):
        apply(setup_files)
    assert path.read_text(encoding="utf-8") == source
    assert not info.exists()
    controller.call.assert_not_called()
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.yaml"]


def test_auth_failure_restores_original_config_and_selectors(setup_files):
    path, source, _, _, controller, info = setup_files
    share.verify_auth.side_effect = RuntimeError("bad authentication")
    with pytest.raises(RuntimeError, match="bad authentication"):
        apply(setup_files)
    assert path.read_text(encoding="utf-8") == source
    assert not info.exists()
    assert [call.args[0] for call in controller.call.call_args_list] == [
        "/configs?force=true", "/proxies/Antigravity",
        "/configs?force=true", "/proxies/Antigravity",
    ]
    assert len(list(path.parent.iterdir())) == 2


def test_reload_failure_attempts_rollback(setup_files):
    path, source, _, _, controller, info = setup_files
    controller.call.side_effect = [TimeoutError("reload timeout"), {}, {}]
    with pytest.raises(TimeoutError):
        apply(setup_files)
    assert path.read_text(encoding="utf-8") == source and not info.exists()
    share.verify_auth.assert_not_called()
    assert controller.call.call_count == 3


def test_all_users_checked_and_credentials_preserved_on_failure(setup_files):
    path, source, _, users, _, info = setup_files
    users.append({"username": "second", "password": "second-password"})
    info.write_text("previous credentials", encoding="utf-8")
    share.verify_auth.side_effect = [None, RuntimeError("second user rejected")]
    with pytest.raises(RuntimeError, match="second user"):
        apply(setup_files)
    assert share.verify_auth.call_count == 2
    assert info.read_text(encoding="utf-8") == "previous credentials"
    assert path.read_text(encoding="utf-8") == source


def test_config_changed_during_password_prompt_is_not_overwritten(setup_files):
    path, _, updated, users, controller, info = setup_files
    path.write_text("newer config", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed while entering"):
        share.apply_config(path, updated, controller, {}, "mihomo", 17895, users, info,
                           expected_source="old config")
    assert path.read_text(encoding="utf-8") == "newer config"
    share.subprocess.run.assert_not_called()
    controller.call.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_cli_sets_multiple_users_and_accepts_existing_credential_file(tmp_path, monkeypatch, capsys, existing):
    original = config()
    if existing:
        original, _, _ = share.listener_config(original, 17895, "proxyuser", "AG-SOCKS5-7894")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    info = tmp_path / "credentials.txt"
    if existing:
        info.write_text("old credentials", encoding="utf-8")
    monkeypatch.setattr(share, "Path", lambda value: info if str(value).startswith("/root/") else Path(value))
    monkeypatch.setattr(share.sys, "argv", ["mihomo_share_proxy.py", "--config", str(path),
        "--address", "proxy.example.com", "--outbound", "AG-SOCKS5-7894", "--set-user", "alice", "--set-user", "bob"])
    monkeypatch.setattr(share.os, "geteuid", lambda: 0, raising=False)
    custom = [{"username": "alice", "password": "alice-password"},
              {"username": "bob", "password": "bob-password"}]
    monkeypatch.setattr(share, "prompt_users", Mock(return_value=custom))
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(share.socket, "socket", Mock(return_value=sock))
    controller = Mock()
    controller.call.return_value = {"proxies": {"Antigravity": {"type": "Selector", "now": "DIRECT"}}}
    monkeypatch.setattr(share, "Controller", Mock(return_value=controller))
    apply_mock = Mock()
    monkeypatch.setattr(share, "apply_config", apply_mock)
    share.main()
    share.prompt_users.assert_called_once_with(["alice", "bob"])
    args = apply_mock.call_args.args
    assert args[6][-2:] == custom
    assert args[7] == info and args[8] == "proxy.example.com"
    assert len(args[6]) == (3 if existing else 2)
    assert sock.bind.call_count == (0 if existing else 1)
    output = capsys.readouterr().out
    assert "alice-password" not in output and "bob-password" not in output


class Socket:
    def __init__(self, replies):
        self.replies = replies
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def recv(self, size):
        result, self.replies = self.replies[:1], self.replies[1:]
        return result

    def sendall(self, message):
        self.sent.append(message)


@pytest.mark.parametrize("no_auth_reply", [b"\x05\x02", b"\x05\xff"])
def test_authentication_accepts_mihomo_handshake(monkeypatch, no_auth_reply):
    sockets = [Socket(no_auth_reply), Socket(b"\x05\x02\x01\x01"),
               Socket(b"\x05\x02\x01\x00")]
    monkeypatch.setattr(share.socket, "create_connection", Mock(side_effect=sockets))
    share.verify_auth(17895, "proxyuser", "saved-secret")
    assert sockets[-1].sent[-1] == b"\x01\x09proxyuser\x0csaved-secret"


def test_authentication_rejects_unprotected_listener(monkeypatch):
    monkeypatch.setattr(share.socket, "create_connection", Mock(return_value=Socket(b"\x05\x00")))
    with pytest.raises(RuntimeError, match="unauthenticated"):
        share.verify_auth(17895, "proxyuser", "saved-secret")


def test_authentication_rejects_server_accepting_wrong_password(monkeypatch):
    monkeypatch.setattr(share.socket, "create_connection", Mock(side_effect=[
        Socket(b"\x05\x02"), Socket(b"\x05\x02\x01\x00")]))
    with pytest.raises(RuntimeError, match="incorrect password"):
        share.verify_auth(17895, "proxyuser", "saved-secret")
