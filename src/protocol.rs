#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Pass(String),
    Nick(String),
    User(String),
    Join(String),
    Part(String),
    Privmsg {
        target: String,
        body: String,
    },
    Ping(String),
    Pong,
    Quit,
    History {
        channel: Option<String>,
        after_seq: i64,
        limit: i64,
    },
    TaskCreate {
        channel: String,
        title: String,
    },
    TaskClaim(String),
    TaskDone(String),
    TaskRelease(String),
    TaskList(String),
    Unknown(String),
}

pub fn parse_command(line: &str) -> Command {
    let line = strip_tags_and_prefix(line.trim());
    let (head, trailing) = split_trailing(line);
    let mut parts = head.split_whitespace();
    let verb = parts.next().unwrap_or_default().to_ascii_uppercase();

    match verb.as_str() {
        "PASS" => Command::Pass(parts.collect::<Vec<_>>().join(" ")),
        "NICK" => Command::Nick(parts.next().unwrap_or_default().to_string()),
        "USER" => Command::User(parts.next().unwrap_or_default().to_string()),
        "JOIN" => Command::Join(parts.next().unwrap_or_default().to_string()),
        "PART" => Command::Part(parts.next().unwrap_or_default().to_string()),
        "PRIVMSG" => Command::Privmsg {
            target: parts.next().unwrap_or_default().to_string(),
            body: trailing.unwrap_or_else(|| parts.collect::<Vec<_>>().join(" ")),
        },
        "PING" => Command::Ping(trailing.unwrap_or_else(|| parts.collect::<Vec<_>>().join(" "))),
        "PONG" => Command::Pong,
        "QUIT" => Command::Quit,
        "CHATHISTORY" => parse_chathistory(parts.collect::<Vec<_>>()),
        "HISTORY" => Command::History {
            channel: Some(parts.next().unwrap_or_default().to_string()),
            after_seq: parts.next().unwrap_or("0").parse().unwrap_or(0),
            limit: parts.next().unwrap_or("100").parse().unwrap_or(100),
        },
        "TASK" => parse_task(parts.collect::<Vec<_>>(), trailing),
        _ => Command::Unknown(verb),
    }
}

fn parse_chathistory(parts: Vec<&str>) -> Command {
    if parts
        .first()
        .is_some_and(|part| part.eq_ignore_ascii_case("AFTER"))
    {
        if parts.get(1).is_some_and(|value| value.starts_with('#')) {
            return Command::History {
                channel: parts.get(1).map(|value| value.to_string()),
                after_seq: parts.get(2).copied().unwrap_or("0").parse().unwrap_or(0),
                limit: parts
                    .get(3)
                    .copied()
                    .unwrap_or("100")
                    .parse()
                    .unwrap_or(100),
            };
        }

        return Command::History {
            channel: None,
            after_seq: parts.get(1).copied().unwrap_or("0").parse().unwrap_or(0),
            limit: parts
                .get(2)
                .copied()
                .unwrap_or("100")
                .parse()
                .unwrap_or(100),
        };
    }

    Command::History {
        channel: parts.first().map(|value| value.to_string()),
        after_seq: parts.get(1).copied().unwrap_or("0").parse().unwrap_or(0),
        limit: parts
            .get(2)
            .copied()
            .unwrap_or("100")
            .parse()
            .unwrap_or(100),
    }
}

fn parse_task(parts: Vec<&str>, trailing: Option<String>) -> Command {
    let Some(action) = parts.first().map(|part| part.to_ascii_uppercase()) else {
        return Command::Unknown("TASK".to_string());
    };

    match action.as_str() {
        "CREATE" => Command::TaskCreate {
            channel: parts.get(1).copied().unwrap_or_default().to_string(),
            title: trailing
                .unwrap_or_else(|| parts.iter().skip(2).copied().collect::<Vec<_>>().join(" ")),
        },
        "CLAIM" => Command::TaskClaim(parts.get(1).copied().unwrap_or_default().to_string()),
        "DONE" => Command::TaskDone(parts.get(1).copied().unwrap_or_default().to_string()),
        "RELEASE" => Command::TaskRelease(parts.get(1).copied().unwrap_or_default().to_string()),
        "LIST" => Command::TaskList(parts.get(1).copied().unwrap_or_default().to_string()),
        _ => Command::Unknown(format!("TASK {action}")),
    }
}

fn strip_tags_and_prefix(line: &str) -> &str {
    let without_tags = if line.starts_with('@') {
        line.split_once(' ').map(|(_, rest)| rest).unwrap_or(line)
    } else {
        line
    };

    if without_tags.starts_with(':') {
        without_tags
            .split_once(' ')
            .map(|(_, rest)| rest)
            .unwrap_or(without_tags)
    } else {
        without_tags
    }
}

fn split_trailing(line: &str) -> (&str, Option<String>) {
    if let Some((head, trailing)) = line.split_once(" :") {
        (head, Some(trailing.to_string()))
    } else {
        (line, None)
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_command, Command};

    #[test]
    fn parses_privmsg_with_trailing_body() {
        assert_eq!(
            parse_command("PRIVMSG #demo :hello world"),
            Command::Privmsg {
                target: "#demo".to_string(),
                body: "hello world".to_string()
            }
        );
    }

    #[test]
    fn parses_task_create() {
        assert_eq!(
            parse_command("TASK CREATE #demo :fix flaky test"),
            Command::TaskCreate {
                channel: "#demo".to_string(),
                title: "fix flaky test".to_string()
            }
        );
    }

    #[test]
    fn parses_chathistory_after() {
        assert_eq!(
            parse_command("CHATHISTORY AFTER 42 100"),
            Command::History {
                channel: None,
                after_seq: 42,
                limit: 100
            }
        );
    }

    #[test]
    fn parses_chathistory_after_channel() {
        assert_eq!(
            parse_command("CHATHISTORY AFTER #demo 42 100"),
            Command::History {
                channel: Some("#demo".to_string()),
                after_seq: 42,
                limit: 100
            }
        );
    }
}
