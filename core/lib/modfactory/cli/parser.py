"""Argument definitions for the `mfactory` command."""

from __future__ import annotations

import argparse

from .. import config

EPILOG = """\
install and maintain
  install | upgrade | uninstall | doctor | status | sync
see the configuration
  config show | graph | models | skills
run jobs (the orchestrator skill uses these)
  queue | create <issue> | next <job> | done <job> | answer <job> <outcome> | summary
other
  checks | dashboard | version
several projects
  fleet subscribe [path] | unsubscribe [path] | list | queue | dashboard

A <job> is its id (004-dark-mode), the number in its id (004) or its issue (12 or #12).
`install` is the same command as `init`.
"""


def parser() -> argparse.ArgumentParser:
    command: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="mfactory", description="A software factory for this repository.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = command.add_subparsers(
        dest="command", metavar="<command>")

    def install_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        installed: argparse.ArgumentParser = sub.add_parser(name, help=help_text)
        installed.add_argument("--tool", action="append", choices=sorted(config.load_adapters()), metavar="TOOL",
                               help="claude, cursor or codex; repeat for several (default: claude)")
        return installed

    install_parser("install", "install the factory into this repository")
    install_parser("init", "install the factory into this repository")
    sync: argparse.ArgumentParser = sub.add_parser("sync", help="write the agent and skill files from factory.toml")
    sync.add_argument("--check", action="store_true", help="change nothing; exit 1 if anything is out of date")
    doctor: argparse.ArgumentParser = sub.add_parser("doctor", help="check that everything a run needs is in place")
    doctor.add_argument("--skip-checks", action="store_true",
                        help="don't run [checks].commands on the base branch")
    upgrade: argparse.ArgumentParser = sub.add_parser("upgrade", help="move .factory/ to another version and re-sync")
    upgrade.add_argument("--version", default="latest", dest="ref",
                         help="a release tag such as v0.2.0 (default: latest)")
    upgrade.add_argument("--source", help="a local copy of the factory to install from, instead of downloading")
    upgrade.add_argument("--self", action="store_true", help="update the global CLI from install.sh --global")
    upgrade.add_argument("--all", action="store_true", help="upgrade every project in the fleet")
    remove: argparse.ArgumentParser = sub.add_parser("uninstall", help="remove the factory from this repository")
    remove.add_argument("--config", action="store_true", help="also delete factory.toml")
    remove.add_argument("--labels", action="store_true", help="also delete the factory labels on GitHub")
    status: argparse.ArgumentParser = sub.add_parser(
        "status", help="show the installed version and whether generated files are current")
    status.add_argument("--all", action="store_true", help="one row per subscribed project")

    show: argparse.ArgumentParser = sub.add_parser("config", help="show the merged configuration")
    show.add_argument("action", choices=["show"])
    show.add_argument("--json", action="store_true")
    graph: argparse.ArgumentParser = sub.add_parser("graph", help="show the pipeline in effect")
    graph.add_argument("--json", action="store_true")
    sub.add_parser("models", help="show the model and effort of each agent, per tool")
    sub.add_parser("skills", help="list the project's skills and which agents read them")

    sub.add_parser("queue", help="what to run now")
    create: argparse.ArgumentParser = sub.add_parser("create", help="start a job for a labeled issue")
    create.add_argument("issue")
    nxt: argparse.ArgumentParser = sub.add_parser("next", help="say what the orchestrator must do for a job")
    nxt.add_argument("job")
    done: argparse.ArgumentParser = sub.add_parser("done", help="read the outcome of the step an agent finished")
    done.add_argument("job")
    done.add_argument("--reply", action="store_true", help="read the agent's reply from standard input")
    answer: argparse.ArgumentParser = sub.add_parser("answer", help="reply to a step that waits for a person")
    answer.add_argument("job")
    answer.add_argument("outcome", help="approve, changes, feedback, answer or reject, as the step allows")
    answer.add_argument("text", nargs="*", help="your words; or pass them on standard input")
    summary: argparse.ArgumentParser = sub.add_parser("summary", help="a table of jobs")
    summary.add_argument("jobs", nargs="*")
    route: argparse.ArgumentParser = sub.add_parser("route", help="show which agents a job uses")
    route.add_argument("job")

    sub.add_parser("checks", help="run [checks].commands in the current directory")
    sub.add_parser("hook")  # no help text: argparse then leaves it out of the list
    board: argparse.ArgumentParser = sub.add_parser("dashboard", help="serve the read-only dashboard on this machine")
    board.add_argument("--port", type=int, default=4747)
    board.add_argument("--no-open", action="store_true", help="don't open a browser")
    board.add_argument("--all", action="store_true", help="one board for every subscribed project")
    fleet: argparse.ArgumentParser = sub.add_parser(
        "fleet", help="open the fleet dashboard; add a command to manage the list")
    fleet.add_argument("--port", type=int, default=4747)
    fleet.add_argument("--no-open", action="store_true", help="don't open a browser")
    fleet_sub: argparse._SubParsersAction[argparse.ArgumentParser] = fleet.add_subparsers(
        dest="fleet_command", required=False, metavar="<command>")
    fleet_sub.add_parser("init", help="write fleet.toml in the current directory")
    subscribe: argparse.ArgumentParser = fleet_sub.add_parser(
        "subscribe", help="add this repository, or a path, to the user fleet")
    subscribe.add_argument("path", nargs="?")
    unsubscribe: argparse.ArgumentParser = fleet_sub.add_parser(
        "unsubscribe", help="remove this repository, or a path, from the fleet")
    unsubscribe.add_argument("path", nargs="?")
    add: argparse.ArgumentParser = fleet_sub.add_parser(
        "add", help="add a checkout where the factory is already installed")
    add.add_argument("path")
    drop: argparse.ArgumentParser = fleet_sub.add_parser("remove", help="remove a project by its path or folder name")
    drop.add_argument("path")
    fleet_sub.add_parser("list", help="list the projects in fleet.toml")
    queue: argparse.ArgumentParser = fleet_sub.add_parser("queue", help="run each project's own factory queue")
    queue.add_argument("--concurrency", type=int, metavar="N",
                       help="fleet-wide cap for new and resume rows (default: [fleet].concurrency)")
    fleet_board: argparse.ArgumentParser = fleet_sub.add_parser(
        "dashboard", help="serve one read-only board for every project")
    fleet_board.add_argument("--port", type=int, default=4747)
    fleet_board.add_argument("--no-open", action="store_true", help="don't open a browser")
    sub.add_parser("version", help="print the factory's version")
    return command
