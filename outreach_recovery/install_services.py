"""Install user LaunchAgents after tests and migrations. Never embeds credentials."""
import argparse,os,plistlib,shutil,subprocess,sys
from pathlib import Path

LABELS={'inbox':'ai.cavaco.outreach.inbox','crm':'ai.cavaco.outreach.crm'}


def configuration(kind,python,code,logs,mailbox,portal):
    label=LABELS[kind]
    args=[str(python),'-u','-m','outreach_recovery.'+('inbox_worker' if kind=='inbox' else 'crm_worker'),'--pilot','--watch']
    args+=['--mailbox',mailbox] if kind=='inbox' else ['--portal',str(portal)]
    return {'Label':label,'ProgramArguments':args,'WorkingDirectory':str(code),
            'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'ProcessType':'Background',
            'ExitTimeOut':15,'Umask':63,
            'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PYTHONPATH':str(code)},
            'StandardOutPath':str(logs/(kind+'.log')),'StandardErrorPath':str(logs/(kind+'.error.log'))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',required=True,type=Path)
    parser.add_argument('--mailbox',required=True)
    parser.add_argument('--portal',required=True,type=int)
    parser.add_argument('--load',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':raise SystemExit('macOS required')
    python=args.python.absolute()
    if not python.is_file() or str(python).startswith(('/tmp/','/private/tmp/')):
        raise SystemExit('Use a persistent Python runtime outside /tmp')
    root=Path.home()/'.local/share/cavaco-outreach'
    code=root/'service-code';logs=root/'logs';agents=Path.home()/'Library/LaunchAgents'
    for directory in (code,logs,agents):directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    uid=str(os.getuid())
    # Stop existing agents before replacing their private code snapshot.
    if args.load:
        for label in LABELS.values():subprocess.run(['launchctl','bootout','gui/'+uid+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    target=code/'outreach_recovery'
    shutil.copytree(Path(__file__).parent,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    entrypoint=python.parent/'sdr'
    entrypoint.write_text('#!'+str(python)+'\nimport sys\nsys.path.insert(0, '+repr(str(code))+')\nfrom outreach_recovery.send_approved import main\nraise SystemExit(main())\n')
    entrypoint.chmod(0o700)
    for kind,label in LABELS.items():
        path=agents/(label+'.plist')
        path.write_bytes(plistlib.dumps(configuration(kind,python,code,logs,args.mailbox,args.portal)))
        path.chmod(0o600)
        for log in (logs/(kind+'.log'),logs/(kind+'.error.log')):
            log.touch(mode=0o600,exist_ok=True);log.chmod(0o600)
        subprocess.run(['plutil','-lint',str(path)],check=True)
        if args.load:subprocess.run(['launchctl','bootstrap','gui/'+uid,str(path)],check=True)
        print('Installed '+str(path))

if __name__=='__main__':main()
