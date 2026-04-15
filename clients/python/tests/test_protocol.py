"""Unit tests for IRC line parser — no server needed."""

from aircd.client import _parse_irc_line, _extract_nick


def test_parse_simple_command():
    prefix, cmd, params, tags = _parse_irc_line("PING :server1")
    assert prefix == ""
    assert cmd == "PING"
    assert params == ["server1"]
    assert tags == {}


def test_parse_prefixed_privmsg():
    line = ":nick!user@host PRIVMSG #channel :hello world"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert prefix == "nick!user@host"
    assert cmd == "PRIVMSG"
    assert params == ["#channel", "hello world"]
    assert tags == {}


def test_parse_numeric_reply():
    line = ":server 001 agent-1 :Welcome to aircd"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert prefix == "server"
    assert cmd == "001"
    assert params == ["agent-1", "Welcome to aircd"]


def test_parse_join():
    line = ":agent-1!agent@aircd JOIN #work"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert prefix == "agent-1!agent@aircd"
    assert cmd == "JOIN"
    assert params == ["#work"]


def test_parse_no_trailing():
    line = "NICK agent-1"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert prefix == ""
    assert cmd == "NICK"
    assert params == ["agent-1"]


def test_parse_with_tags():
    line = "@seq=42;time=2026-01-01 :nick!user@host PRIVMSG #ch :tagged msg"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert prefix == "nick!user@host"
    assert cmd == "PRIVMSG"
    assert params == ["#ch", "tagged msg"]
    assert tags == {"seq": "42", "time": "2026-01-01"}


def test_parse_tags_with_replay():
    line = "@seq=100;replay=1 :bot!bot@aircd PRIVMSG #ch :replayed message"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert tags["seq"] == "100"
    assert tags["replay"] == "1"
    assert params == ["#ch", "replayed message"]


def test_parse_tags_boolean_flag():
    line = "@seq=5;batch :nick!u@h PRIVMSG #ch :batched"
    prefix, cmd, params, tags = _parse_irc_line(line)
    assert tags == {"seq": "5", "batch": ""}


def test_extract_nick_full():
    assert _extract_nick("nick!user@host") == "nick"


def test_extract_nick_plain():
    assert _extract_nick("nick") == "nick"


def test_extract_nick_empty():
    assert _extract_nick("") == ""
