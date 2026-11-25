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
  queue | create <issue> | next <job> | done <job> | answer <job> <outcome>
  close issue <job> | close job <job> [--merged <pr>] | move <job> <step> [note]
other
  checks | dashboard | start dashboard | stop | version
several projects
  workspace add [path] | remove [path] | list | queue | init

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
        installed.add_argument("--personal", action="store_true",
                               help="keep factory.toml, .factory/ and the generated files untracked in this checkout")
        return installed

    install_parser("install", "install the factory into this repository")
    install_parser("init", "install the factory into this repository")
    sync: argparse.ArgumentParser = sub.add_parser("sync", help="write the agent and skill files from factory.toml")
    sync.add_argument("--check", action="store_true", help="change nothing; exit 1 if anything is out of date")
    doctor: argparse.ArgumentParser = sub.add_parser("doctor", help="check that everything a run needs is in place")
    doctor.add_argument("--skip-checks", action="store_true",
                        help="don't run [checks].commands on the base branch")
    upgrade: argparse.ArgumentParser = sub.add_parser(
        "upgrade", help="upgrade the global CLI and every installed project")
    upgrade.add_argument("--version", default="latest", dest="ref",
                         help="a release tag such as v0.2.0 (default: latest)")
    upgrade.add_argument("--source", help="a local copy of the factory to install from, instead of downloading")
    upgrade.add_argument("--self", action="store_true", help="update only the global CLI")
    upgrade.add_argument("--all", action="store_true", help="upgrade only the projects in the workspace")
    upgrade.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    remove: argparse.ArgumentParser = sub.add_parser("uninstall", help="remove the factory from this repository")
    remove.add_argument("--config", action="store_true", help="also delete factory.toml")
    remove.add_argument("--labels", action="store_true", help="also delete the factory labels on GitHub")
    status: argparse.ArgumentParser = sub.add_parser(
        "status", help="show the installed version and whether generated files are current")
    status.add_argument("--all", action="store_true", help="one row per project in the workspace")

    show: argparse.ArgumentParser = sub.add_parser("config", help="show the merged configuration")
    show.add_argument("action", choices=["show"])
    show.add_argument("--json", action="store_true")
    graph: argparse.ArgumentParser = sub.add_parser("graph", help="show the pipeline in effect")
    graph.add_argument("--json", action="store_true")
    sub.add_parser("models", help="show the model and effort of each agent, per tool")
    sub.add_parser("skills", help="list skills and instruction files, and which agents read them")

    queue_run: argparse.ArgumentParser = sub.add_parser("queue", help="what to run now")
    queue_run.add_argument("--json", action="store_true",
                           help="one JSON array, with each issue's title, URL, dates, author and labels")
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
    close: argparse.ArgumentParser = sub.add_parser("close", help="close a job's issue, or end a job, by hand")
    close_sub: argparse._SubParsersAction[argparse.ArgumentParser] = close.add_subparsers(
        dest="close_what", required=True, metavar="<issue|job>")
    close_issue: argparse.ArgumentParser = close_sub.add_parser(
        "issue", help="close the job's issue on GitHub; the job is not touched")
    close_issue.add_argument("job")
    close_issue.add_argument("--not-planned", action="store_true", help="close as not planned, not completed")
    close_job: argparse.ArgumentParser = close_sub.add_parser(
        "job", help="end the job as done from any step; the issue stays open")
    close_job.add_argument("job")
    close_job.add_argument("--merged", metavar="PR", type=int,
                           help="the pull request that merged this work, if not the job's own; recorded on the job")
    move: argparse.ArgumentParser = sub.add_parser(
        "move", help="put an unfinished job at any enabled step, outside the graph's edges")
    move.add_argument("job")
    move.add_argument("step", help="the step to put the job at, as factory.toml names it")
    move.add_argument("text", nargs="*", help="a note for the next agent; or pass it on standard input")
    route: argparse.ArgumentParser = sub.add_parser("route", help="show which agents a job uses")
    route.add_argument("job")

    sub.add_parser("checks", help="run [checks].commands in the current directory")
    sub.add_parser("hook")  # no help text: argparse then leaves it out of the list
    line: argparse.ArgumentParser = sub.add_parser("statusline")  # Claude Code's status line runs this
    line.add_argument("--then", help="the status line command to run after recording")
    def dashboard_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("--port", type=int, default=4747)
        target.add_argument("--no-open", action="store_true", help="don't open a browser")

    board: argparse.ArgumentParser = sub.add_parser(
        "dashboard", help="serve the read-only dashboard for every project in the workspace")
    dashboard_flags(board)
    start: argparse.ArgumentParser = sub.add_parser("start", help="start a service in the background")
    start_sub: argparse._SubParsersAction[argparse.ArgumentParser] = start.add_subparsers(
        dest="service", required=True, metavar="<service>")
    start_board: argparse.ArgumentParser = start_sub.add_parser(
        "dashboard", help="serve the dashboard and return")
    dashboard_flags(start_board)
    stop: argparse.ArgumentParser = sub.add_parser("stop", help="stop the background dashboard")
    stop.add_argument("service", nargs="?", choices=["dashboard"], default="dashboard")
    workspace: argparse.ArgumentParser = sub.add_parser(
        "workspace", help="the projects this machine's dashboard and queue cover")
    workspace_sub: argparse._SubParsersAction[argparse.ArgumentParser] = workspace.add_subparsers(
        dest="workspace_command", required=False, metavar="<command>")
    workspace_sub.add_parser("init", help="write workspace.toml in the current directory")
    add: argparse.ArgumentParser = workspace_sub.add_parser(
        "add", help="add this repository, or a path, to the workspace")
    add.add_argument("path", nargs="?")
    drop: argparse.ArgumentParser = workspace_sub.add_parser(
        "remove", help="remove this repository, or a project by its path or folder name")
    drop.add_argument("path", nargs="?")
    workspace_sub.add_parser("list", help="list the projects in workspace.toml")
    queue: argparse.ArgumentParser = workspace_sub.add_parser("queue", help="run each project's own factory queue")
    queue.add_argument("--concurrency", type=int, metavar="N",
                       help="cap for new and resume rows across projects (default: [workspace].concurrency)")
    sub.add_parser("version", help="print the factory's version")
    return command
