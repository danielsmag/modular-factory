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
  file <title>              open an issue in the queue; its text is read from standard input
  close issue <job> | close job <job> [--merged <pr>] | move <job> <step> [note]
  delete <job> [--worktree] [--branch] [--remote] [--pr] [--force]
  tracker show [--json] | tracker sync | tracker suggest [--json]
plan the queue across jobs
  plan snapshot [--json] [--label NAME]
  plan collect [--json]
  plan scan [--force] | plan due | plan ingest [--reply]
  plan release <ref>
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
    graph: argparse.ArgumentParser = sub.add_parser("graph", help="show a pipe in effect")
    graph.add_argument("--json", action="store_true")
    graph.add_argument("--pipe", "--pipeline", dest="pipe", metavar="NAME", help="the pipe to show; default: default")
    # `pipelines` is the name this command had before it was released as `pipes`.
    pipes: argparse.ArgumentParser = sub.add_parser("pipes", aliases=["pipelines"],
                                                    help="list, show or validate the YAML pipes in factory/pipes/")
    pipes_sub = pipes.add_subparsers(dest="pipes_what", required=True)
    pipes_list: argparse.ArgumentParser = pipes_sub.add_parser("list", help="every pipe, its file and steps")
    pipes_list.add_argument("--json", action="store_true",
                            help="one JSON object: the pipes, [run].pipe, and whether to ask which to use")
    pipes_show: argparse.ArgumentParser = pipes_sub.add_parser("show", help="one pipe after extends, as YAML")
    pipes_show.add_argument("name")
    pipes_sub.add_parser("validate", help="check every pipe file against the schema and the graph rules")
    pipes_sub.add_parser("schema", help="print the JSON Schema of a pipe file")
    migrate: argparse.ArgumentParser = sub.add_parser("migrate", help="move old settings to their new home")
    migrate.add_argument("what", choices=["layout", "pipes"],
                         help="layout: factory.toml at the root into factory/; "
                              "pipes: [steps.*] in factory.toml to factory/pipes/default.yaml")
    sub.add_parser("models", help="show the model and effort of each agent, per tool")
    sub.add_parser("skills", help="list skills and instruction files, and which agents read them")

    queue_run: argparse.ArgumentParser = sub.add_parser("queue", help="what to run now")
    queue_run.add_argument("--json", action="store_true",
                           help="one JSON array, with each issue's title, URL, dates, author and labels")
    create: argparse.ArgumentParser = sub.add_parser("create", help="start a job for a labeled issue")
    create.add_argument("issue")
    create.add_argument("--pipe", "--pipeline", dest="pipe", metavar="NAME",
                        help="the pipe the job runs; default: [run].pipe, or the only pipe")
    file: argparse.ArgumentParser = sub.add_parser(
        "file", help="open an issue in the queue; its text is read from standard input")
    file.add_argument("--tracker", choices=config.TRACKER_KINDS,
                      help="with two trackers, where to open it; the first in [tracker].kind otherwise")
    file.add_argument("title", nargs="+")
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
    delete: argparse.ArgumentParser = sub.add_parser(
        "delete", help="remove a job folder and rewrite the dashboard index")
    delete.add_argument("job")
    delete.add_argument("--worktree", action="store_true", help="remove the job's worktrees")
    delete.add_argument("--branch", action="store_true",
                        help="delete the local branch; also removes the worktree")
    delete.add_argument("--remote", action="store_true", help="delete the branch on origin")
    delete.add_argument("--pr", action="store_true", help="close the job's open pull request")
    delete.add_argument("--force", action="store_true",
                        help="worktree, local branch, remote branch, and the open pull request")

    tracker: argparse.ArgumentParser = sub.add_parser(
        "tracker", help="the steps against the tracker's board, and cards put back in place")
    tracker_sub: argparse._SubParsersAction[argparse.ArgumentParser] = tracker.add_subparsers(
        dest="tracker_what", required=True, metavar="<show|sync|suggest>")
    tracker_show: argparse.ArgumentParser = tracker_sub.add_parser(
        "show", help="each step's status and board column, and what is wrong with them")
    tracker_show.add_argument("--json", action="store_true", help="the report as one JSON object")
    tracker_sub.add_parser("sync", help="move every unfinished job's card to its step's status")
    tracker_suggest: argparse.ArgumentParser = tracker_sub.add_parser(
        "suggest", help="the Jira board that works well for this pipeline, and its [tracker.jira.columns]")
    tracker_suggest.add_argument("--json", action="store_true", help="the suggestion as one JSON object")

    plan: argparse.ArgumentParser = sub.add_parser(
        "plan", help="the planner: duplicates, dependencies and priority across the queue")
    plan_sub: argparse._SubParsersAction[argparse.ArgumentParser] = plan.add_subparsers(
        dest="plan_what", required=True, metavar="<snapshot|collect|scan|due|ingest|release>")
    plan_snapshot: argparse.ArgumentParser = plan_sub.add_parser(
        "snapshot", help="every queued issue and job of this project, as tickets")
    plan_snapshot.add_argument("--json", action="store_true", help="the snapshot as one JSON object")
    plan_snapshot.add_argument("--label", help="the project's name in refs; the repository's folder by default")
    plan_collect: argparse.ArgumentParser = plan_sub.add_parser(
        "collect", help="every project of the workspace as one snapshot, with candidate pairs")
    plan_collect.add_argument("--json", action="store_true", help="the snapshot as one JSON object")
    plan_scan: argparse.ArgumentParser = plan_sub.add_parser(
        "scan", help="collect, then say whether the scan agent should run")
    plan_scan.add_argument("--force", action="store_true", help="scan even if nothing changed or it is not time")
    plan_sub.add_parser("due", help="print scan or not due")
    plan_ingest: argparse.ArgumentParser = plan_sub.add_parser(
        "ingest", help="check the scan agent's draft and merge it into the findings")
    plan_ingest.add_argument("--reply", action="store_true", help="read the draft from standard input")
    plan_release: argparse.ArgumentParser = plan_sub.add_parser(
        "release", help="run a held job or issue now, without waiting for the job it waits on")
    plan_release.add_argument("ref", help="a job id, or an issue key such as 12, #12 or PROJ-7")

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
