"""The `factory` command line."""

import argparse
import json
import os
import sys

from . import checks, config, graph, install
from . import dashboard as dash
from . import sync as syncer
from .engine import Factory
from .store import Store
from .util import ConfigError, FactoryError, find_repo, version

EPILOG = """\
install and maintain
  init | sync | doctor | upgrade
see the configuration
  config show | graph | models | skills
run jobs (the orchestrator skill uses these)
  queue | create <issue> | next <job> | done <job> | answer <job> <outcome> | summary
other
  checks | dashboard | version

A <job> is its id (004-dark-mode), the number in its id (004) or its issue (12 or #12).
"""


def parser():
    p = argparse.ArgumentParser(prog="factory", description="A software factory for this repository.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    c = sub.add_parser("init", help="install the factory into this repository")
    c.add_argument("--tool", action="append", choices=sorted(config.load_adapters()), metavar="TOOL",
                   help="claude, cursor or codex; repeat for several (default: claude)")
    c = sub.add_parser("sync", help="write the agent and skill files from factory.toml")
    c.add_argument("--check", action="store_true", help="change nothing; exit 1 if anything is out of date")
    c = sub.add_parser("doctor", help="check that everything a run needs is in place")
    c.add_argument("--skip-checks", action="store_true", help="don't run [checks].commands on the base branch")
    c = sub.add_parser("upgrade", help="move .factory/ to another version and re-sync")
    c.add_argument("--version", default="latest", dest="ref", help="a release tag such as v0.2.0 (default: latest)")
    c.add_argument("--source", help="a local copy of the factory to install from, instead of downloading")

    c = sub.add_parser("config", help="show the merged configuration")
    c.add_argument("action", choices=["show"])
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("graph", help="show the pipeline in effect")
    c.add_argument("--json", action="store_true")
    sub.add_parser("models", help="show the model and effort of each agent, per tool")
    sub.add_parser("skills", help="list the project's skills and which agents read them")

    sub.add_parser("queue", help="what to run now")
    c = sub.add_parser("create", help="start a job for a labeled issue")
    c.add_argument("issue")
    c = sub.add_parser("next", help="say what the orchestrator must do for a job")
    c.add_argument("job")
    c = sub.add_parser("done", help="read the outcome of the step an agent finished")
    c.add_argument("job")
    c.add_argument("--reply", action="store_true", help="read the agent's reply from standard input")
    c = sub.add_parser("answer", help="reply to a step that waits for a person")
    c.add_argument("job")
    c.add_argument("outcome", help="approve, changes, feedback, answer or reject, as the step allows")
    c.add_argument("text", nargs="*", help="your words; or pass them on standard input")
    c = sub.add_parser("summary", help="a table of jobs")
    c.add_argument("jobs", nargs="*")
    c = sub.add_parser("route", help="show which agents a job uses")
    c.add_argument("job")

    sub.add_parser("checks", help="run [checks].commands in the current directory")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    c = sub.add_parser("dashboard", help="serve the read-only dashboard on this machine")
    c.add_argument("--port", type=int, default=4747)
    c.add_argument("--no-open", action="store_true", help="don't open a browser")
    sub.add_parser("version", help="print the factory's version")
    return p


def _stale_note(cfg):
    """Agents run from the generated files, so say when they no longer match factory.toml."""
    try:
        actions, _ = syncer.sync(cfg, check=True)
    except ConfigError:
        return
    if actions:
        print(f"note: {len(actions)} generated file(s) are out of date with factory.toml. "
              "Run `.factory/bin/factory sync` and commit the result.", flush=True)


def _stdin_text():
    return "" if sys.stdin.isatty() else sys.stdin.read()


def run(args):
    command = args.command
    if command == "version":
        print(version())
        return 0
    if command == "hook":
        repo = find_repo(os.environ.get("CLAUDE_PROJECT_DIR"))
        cfg = config.load(repo)
        return checks.hook(cfg, Store(cfg))

    repo = find_repo()
    if command == "init":
        install.init(repo, args.tool)
        return 0
    if command == "upgrade":
        install.upgrade(repo, args.ref, args.source)
        return 0
    if command == "doctor":
        return 1 if install.doctor(repo, run_checks=not args.skip_checks) else 0

    cfg = config.load(repo)
    if command == "config":
        print(json.dumps(config.as_json(cfg), indent=1) if args.json else config.show(cfg),
              end="\n" if args.json else "")
    elif command == "graph":
        print(json.dumps(graph.as_json(cfg), indent=1) if args.json else graph.describe(cfg),
              end="\n" if args.json else "")
    elif command == "models":
        print(syncer.describe_models(cfg), end="")
    elif command == "skills":
        print(syncer.describe_skills(cfg), end="")
    elif command == "sync":
        actions, notes = syncer.sync(cfg, check=args.check)
        for action, rel in actions:
            print(f"{'would ' if args.check else ''}{action}  {rel}")
        for note in notes:
            print(f"note: {note}")
        if not actions:
            print(f"up to date: {len(syncer.plan(cfg)[0])} generated files for {', '.join(cfg.tools)}")
        return 1 if args.check and actions else 0
    elif command == "checks":
        return checks.run_here(cfg)
    elif command == "dashboard":
        dash.serve(repo, args.port, not args.no_open)
    else:
        factory = Factory(cfg)
        if command == "queue":
            _stale_note(cfg)
            rows = factory.queue()
            for row in rows:
                print("  ".join(str(cell) for cell in row))
            if not rows:
                print(f"nothing to do: no open issue is labeled `{cfg['labels']['queue']}` and no job is unfinished")
        elif command == "create":
            job = factory.create(args.issue)
            print(f"created {job['id']} for #{job['issue']} at {job['step']}")
        elif command == "next":
            factory.next(args.job)
        elif command == "done":
            factory.done(args.job, reply=sys.stdin.read() if args.reply else None)
        elif command == "answer":
            factory.answer(args.job, args.outcome, " ".join(args.text) or _stdin_text())
        elif command == "summary":
            print(factory.summary(args.jobs))
        elif command == "route":
            job = factory.store.read(factory.store.find(args.job))
            print(json.dumps(job["agents"] or factory.route(dict(job)), indent=1))
    return 0


def main(argv):
    p = parser()
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        return 0
    try:
        return run(args)
    except ConfigError as e:
        print(f"factory: {config.FILE} has {len(e.problems)} problem(s):", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    except FactoryError as e:
        print(f"factory: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
