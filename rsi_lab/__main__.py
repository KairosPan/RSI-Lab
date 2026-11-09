"""Task-independent CLI. Domain factories are explicit trusted host arguments."""
import argparse
import importlib
import json
from pathlib import Path
import sys

from .controller import Controller
from .plans import register_task_plan
from .roles import CodexExecutor
from .runtime import Runtime
from .store import Store


def read(path):
    return json.loads(Path(path).read_text())


def load_adapter_factory(reference):
    if ':' not in reference:
        raise ValueError('A trusted adapter factory must be named as package.module:function')
    module,name=reference.split(':',1)
    if not module or not name.isidentifier():
        raise ValueError('Invalid adapter factory identity')
    return getattr(importlib.import_module(module),name)()


def main(argv=None):
    p=argparse.ArgumentParser(description='Task-independent RSI-Lab controller')
    p.add_argument('--root',required=True);p.add_argument('--run',default='run1')
    p.add_argument('--adapter-factory',action='append',default=[],
        help='Trusted installed Python factory, explicitly chosen by the operator; may be repeated')
    sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('init');q.add_argument('--config',required=True)
    for name in ('status','recover','tick'):sub.add_parser(name)
    q=sub.add_parser('register-plan');q.add_argument('--plan',required=True)
    q=sub.add_parser('adapter-run');q.add_argument('--adapter',required=True)
    q.add_argument('--inputs',required=True);q.add_argument('--event',required=True)
    for name in ('meta-step', 'lab-step'):
        q=sub.add_parser(name);q.add_argument('--work',required=True)
    q=sub.add_parser('lab-start');q.add_argument('--event',required=True)
    q.add_argument('--evidence',action='append',default=[])
    q=sub.add_parser('agent-checkpoint');q.add_argument('--session',required=True)
    q=sub.add_parser('agent-rebuild');q.add_argument('--session',required=True)
    q.add_argument('--reason',required=True);q.add_argument('--checkpoint')
    q=sub.add_parser('finalize');q.add_argument('--reason',required=True)
    q=sub.add_parser('run-until-wait');q.add_argument('--max-steps',type=int,default=20)
    q=sub.add_parser('cancel-prepared');q.add_argument('--effect',required=True);q.add_argument('--reason',required=True)
    q=sub.add_parser('reply');q.add_argument('--question',required=True);q.add_argument('--version',type=int,required=True)
    q.add_argument('--text',required=True);q.add_argument('--message-id',required=True)
    q=sub.add_parser('deliver');q.add_argument('--request',required=True);q.add_argument('--content',required=True)
    q.add_argument('--provenance',required=True);q.add_argument('--source-version',type=int,required=True)
    q.add_argument('--page',type=int,default=0);q.add_argument('--kind',choices=['raw','summary','pointer'],default='raw')
    args=p.parse_args(argv)
    adapters=[load_adapter_factory(ref) for ref in args.adapter_factory]
    store=Store(args.root)
    try:
        controller=Controller(store)
        if args.command=='init':
            config=read(args.config);budgets=config.pop('budgets');capacity=config.pop('max_external',1)
            result=controller.initialize(args.run,budgets=budgets,max_external=capacity,config=config)
        else:
            runtime=Runtime(store,args.run,adapters=adapters)
            if args.command=='status':result=runtime.status()
            elif args.command=='recover':result=runtime.recover()
            elif args.command=='tick':result=controller.tick(args.run)
            elif args.command=='lab-start':
                result=runtime.enqueue_lab(args.event,args.evidence)
            elif args.command=='agent-checkpoint':
                session=store.get(args.run,'agent_session',args.session)
                result=runtime.agents.seal_checkpoint(args.session,'checkpoint:'+args.session+':'+str(session['committed_turn']))
            elif args.command=='agent-rebuild':
                session=store.get(args.run,'agent_session',args.session)
                if session['active_turn_id']:
                    runtime.agents.discard_completed_turn(args.session,args.reason,
                        'discard-for-rebuild:'+session['active_turn_id'])
                result=runtime.agents.rebuild_session(args.session,args.reason,'rebuild:'+args.session,
                    checkpoint_id=args.checkpoint)
            elif args.command=='finalize':result=runtime.finalize(args.reason)
            elif args.command=='register-plan':
                plan=read(args.plan)
                result=register_task_plan(store,args.run,plan,key='task-plan:'+plan['task_id'])
            elif args.command=='adapter-run':
                result=runtime.execute_adapter(args.adapter,read(args.inputs),trigger_id=args.event)
            elif args.command=='cancel-prepared':
                result=controller.cancel_prepared(args.run,args.effect,args.reason,'cancel:'+args.effect)
            elif args.command=='reply':
                result=controller.reply(args.run,args.question,args.version,args.text,'user-message:'+args.message_id)
                controller.tick(args.run)
            elif args.command=='deliver':
                result=runtime.ingest_delivery(args.request,read(args.content),read(args.provenance),
                    kind=args.kind,page=args.page,source_version=args.source_version)
            else:
                config=store.run(args.run)['config']
                if config.get('execution_mode') not in {'live','role_recall'}:
                    raise ValueError('Real model execution needs a prospectively configured live or role_recall run')
                executor=CodexExecutor(store.root/'calls')
                if args.command in {'meta-step','lab-step'}:
                    run_step=runtime.run_lab if args.command=='lab-step' else runtime.run_meta
                    result=run_step(args.work,executor,config['model_reservation'])
                else:
                    result=runtime.run_until_wait(executor,config['model_reservation'],max_steps=args.max_steps)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2))
    finally:
        store.close()
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,KeyError,OSError,ImportError,AttributeError) as error:
        print(json.dumps({'status':'blocked','reason':str(error)},ensure_ascii=False),file=sys.stderr)
        sys.exit(2)
