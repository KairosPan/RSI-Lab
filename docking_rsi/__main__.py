"""Explicit durable commands; no hidden daemon or automatic unknown-call retry."""
import argparse
import json
from pathlib import Path
import sys

from .store import Store
from .controller import Controller
from .runtime import Runtime


def read(path):
    return json.loads(Path(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Docking RSI-Lab v2 durable controller')
    parser.add_argument('--root', required=True, help='Independent v2 ledger/artifact directory')
    parser.add_argument('--run', default='run1')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init'); p.add_argument('--config', required=True)
    for name in ('status', 'recover', 'tick', 'demo'):
        sub.add_parser(name)
    p = sub.add_parser('meta-step', help='Dispatch one eligible Meta Work using the existing Codex transport')
    p.add_argument('--work', help='Default: next eligible Meta Work')
    p = sub.add_parser('run-until-wait', help='Advance eligible Meta work until a dependency, budget or step boundary')
    p.add_argument('--max-steps',type=int,default=20)
    p = sub.add_parser('reply')
    p.add_argument('--question', required=True); p.add_argument('--version', type=int, required=True)
    p.add_argument('--text', required=True); p.add_argument('--message-id', required=True)
    p = sub.add_parser('deliver')
    p.add_argument('--request', required=True); p.add_argument('--content', required=True)
    p.add_argument('--provenance', required=True); p.add_argument('--kind', choices=['raw','summary','pointer'], default='raw')
    p.add_argument('--page', type=int, default=0); p.add_argument('--source-version', type=int, required=True)
    p = sub.add_parser('register-study'); p.add_argument('--plan', required=True)
    p = sub.add_parser('select-study'); p.add_argument('--study', required=True); p.add_argument('--arm', required=True)
    p = sub.add_parser('cancel-prepared'); p.add_argument('--effect', required=True); p.add_argument('--reason', required=True)
    p = sub.add_parser('inspect-docking')
    p.add_argument('--data', required=True); p.add_argument('--protocol', required=True)
    p.add_argument('--sources', help='JSON mapping of explicitly authorized supplemental sources')
    p.add_argument('--scope', choices=['exploratory','formal'], default='exploratory')
    p.add_argument('--event', required=True)
    p = sub.add_parser('role-probe', help='At most two real Meta calls on frozen historical originals')
    p.add_argument('--sources', required=True); p.add_argument('--model', default='gpt-6-astra')
    p.add_argument('--goal', default='Screen the supplied compounds against the receptor using AutoDock Vina.')
    args = parser.parse_args(argv)
    if args.command == 'demo':
        from .demo import run_demo
        result = run_demo(args.root, run_id=args.run)
    elif args.command == 'role-probe':
        from .role_probe import run_probe
        result = run_probe(args.root, read(args.sources), model=args.model, goal=args.goal)
    else:
        store = Store(args.root)
        try:
            c = Controller(store)
            if args.command == 'init':
                config = read(args.config)
                budgets = config.pop('budgets'); max_external = config.pop('max_external', 1)
                result = c.initialize(args.run, budgets=budgets, max_external=max_external, config=config)
            else:
                runtime = Runtime(store, args.run)
                if args.command == 'status': result = runtime.status()
                elif args.command == 'recover': result = runtime.recover()
                elif args.command == 'tick': result = c.tick(args.run)
                elif args.command == 'cancel-prepared':
                    result = c.cancel_prepared(args.run, args.effect, args.reason, 'cancel-prepared:' + args.effect)
                elif args.command == 'inspect-docking':
                    result = runtime.inspect_docking(args.data, args.protocol, sources=read(args.sources) if args.sources else None,
                                                     scope=args.scope, trigger_id=args.event)
                elif args.command == 'reply':
                    result = c.reply(args.run, args.question, args.version, args.text, 'user-message:' + args.message_id)
                    c.tick(args.run)
                elif args.command == 'deliver':
                    result = runtime.ingest_delivery(args.request, read(args.content), read(args.provenance),
                        kind=args.kind, page=args.page, source_version=args.source_version)
                elif args.command in {'meta-step','run-until-wait'}:
                    from .roles import CodexExecutor
                    config = store.run(args.run)['config']
                    if config.get('execution_mode') not in {'live', 'role_recall'}:
                        raise ValueError('Real Meta execution requires a prospectively configured live or role_recall run')
                    executor = CodexExecutor(store.root / 'calls')
                    if args.command == 'run-until-wait':
                        result = runtime.run_until_wait(executor, config['model_reservation'], max_steps=args.max_steps)
                    else:
                        work_id = args.work
                        if not work_id:
                            eligible = [w for w in c.ready(args.run) if w['kind'] in {'meta_discovery','meta_disposition','disposition'}]
                            if not eligible:
                                raise ValueError('No eligible Meta Work; inspect status for concrete dependencies or blocked effects')
                            work_id = eligible[0]['id']
                        result = runtime.run_meta(work_id, executor, config['model_reservation'])
                elif args.command == 'register-study':
                    from .study import register_study
                    plan = read(args.plan)
                    result = register_study(store, args.run, plan, key='register-study:' + plan['study_id'])
                elif args.command == 'select-study':
                    from .study import select_study
                    result = select_study(store, args.run, args.study, args.arm)
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
