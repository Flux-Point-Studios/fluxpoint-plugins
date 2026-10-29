#!/usr/bin/env python3
"""Native hook contracts; --live also runs real Claude without model calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import signal
import threading

PLUGIN = Path(__file__).resolve().parents[1]
ENTRY = PLUGIN / 'scripts/native-hook.mjs'
LIVE = '--live' in sys.argv
if LIVE: sys.argv.remove('--live')


class NativeHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, FPL_PY=sys.executable, XDG_CACHE_HOME=str(self.root/'cache'), CLAUDE_PROJECT_DIR=str(self.root))
        self.env.pop('FPL_DISABLE', None)
        self.env['FPL_CLAUDE_BINARY'] = os.environ.get('FPL_CLAUDE_BINARY') or shutil.which('claude') or ''
        subprocess.run(['git','init','-q',str(self.root)],check=True)

    def invoke(self, mode, payload=None, **kwargs):
        return subprocess.run(['node',str(ENTRY),mode], input=json.dumps(payload or {}), cwd=self.root,
                              env=kwargs.pop('env',self.env), capture_output=True,text=True,timeout=15,**kwargs)

    def test_manifest_native_claude_and_explicit_codex_legacy(self):
        claude=json.loads((PLUGIN/'hooks/hooks.json').read_text(encoding='utf-8'))['hooks']
        codex_path=json.loads((PLUGIN/'plugin.json').read_text(encoding='utf-8'))['extensions']['com.openai']['hooks']
        codex=json.loads((PLUGIN/codex_path).read_text(encoding='utf-8'))['hooks']
        for event,index,mode in [('PreToolUse',0,'guard'),('PostToolUse',1,'attest'),('PostToolUseFailure',0,'attest')]:
            hook=claude[event][index]['hooks'][0]
            self.assertEqual(hook['command'],'node');self.assertEqual(hook['args'],['${CLAUDE_PLUGIN_ROOT}/scripts/native-hook.mjs',mode])
            self.assertNotIn('args',codex[event][index]['hooks'][0])
            self.assertIn('bash ',codex[event][index]['hooks'][0]['command'])
        self.assertEqual(claude['PreToolUse'][0]['hooks'][0]['onFailure'],'block')

    def test_secret_policy_and_missing_interpreter_fail_closed(self):
        (self.root/'.fluxpoint-secrets.json').write_text(json.dumps([{'id':'wallet','paths':['wallet.txt'],'why':'fixture private wallet'}]))
        payload={'cwd':str(self.root),'tool_name':'Bash','tool_input':{'command':'cat wallet.txt'}}
        direct=subprocess.run([sys.executable,str(PLUGIN/'scripts/secret-guard.py'),'--hook'],input=json.dumps(payload),cwd=self.root,env=self.env,capture_output=True,text=True)
        native=self.invoke('guard',payload)
        self.assertEqual(native.returncode,direct.returncode);self.assertEqual(native.stdout,direct.stdout)
        self.assertIn('deny',native.stdout)
        bad=self.invoke('guard',payload,env=dict(self.env,FPL_PY=str(self.root/'missing-python')))
        self.assertEqual(bad.returncode,2);self.assertIn('interpreter',bad.stderr)

    def test_input_limit_and_open_stdin_are_bounded(self):
        huge=subprocess.run(['node',str(ENTRY),'guard'],input='x'*(4*1024*1024+1),cwd=self.root,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(huge.returncode,2)
        proc=subprocess.Popen(['node',str(ENTRY),'guard'],cwd=self.root,env=self.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        start=time.monotonic();self.assertEqual(proc.wait(timeout=8),2);self.assertLess(time.monotonic()-start,7)
        proc.stdin.close();proc.stdout.close();proc.stderr.close()

    def test_runtime_minimum_and_mjs_identity_are_enforced(self):
        script="import {supportedClaude} from " + json.dumps(ENTRY.as_uri()) + ";console.log(JSON.stringify(['2.1.139','2.1.311','2.1.312','2.2.0'].map(supportedClaude)));"
        result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),[False,False,True,True])
        sys.path.insert(0,str(PLUGIN/'scripts'));import plugin_identity
        copy=self.root/'plugin';shutil.copytree(PLUGIN,copy)
        first=plugin_identity.current(copy);entry=copy/'scripts/native-hook.mjs';entry.write_text(entry.read_text(encoding='utf-8')+'\n// changed\n')
        self.assertNotEqual(first['scriptsSha256'],plugin_identity.current(copy)['scriptsSha256'])

    def test_attest_is_advisory_and_warns_once_in_payload_project(self):
        (self.root/'.fluxpoint-gates.json').write_text('{broken')
        payload={'session_id':'test-session','cwd':str(self.root),'tool_name':'Bash','tool_input':{'command':'bash scripts/harness.sh --full'},'tool_response':{'stdout':'','stderr':'','exit_code':0}}
        first=self.invoke('attest',payload);second=self.invoke('attest',payload)
        self.assertEqual(first.returncode,0);self.assertEqual(second.returncode,0)
        self.assertIn('DISARMED',first.stdout);self.assertEqual(second.stdout,'')

    def test_null_and_malformed_attest_payload_remain_advisory(self):
        for raw in ('null','[]','not json'):
            result=subprocess.run(['node',str(ENTRY),'attest'],input=raw,cwd=self.root,env=self.env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
        repeated=subprocess.run(['node',str(ENTRY),'attest'],input='null',cwd=self.root,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(repeated.stdout,'')

    def test_child_deadline_and_full_output_drain(self):
        module=json.dumps(ENTRY.as_uri())
        script="import {run} from "+module+";const r=await run(process.argv[1],['-c',\"import sys;sys.stdout.write('x'*200000)\"],'',2000);console.log(r.stdout.length);"
        result=subprocess.run(['node','--input-type=module','-e',script,sys.executable],capture_output=True,text=True,timeout=5,check=True)
        self.assertEqual(result.stdout.strip(),'200000')
        script="import {run} from "+module+";try{await run(process.argv[1],['-c','import time;time.sleep(10)'],'',100);process.exitCode=1}catch(e){console.log(e.message)}"
        result=subprocess.run(['node','--input-type=module','-e',script,sys.executable],capture_output=True,text=True,timeout=5,check=True)
        self.assertIn('deadline',result.stdout)

    def test_auto_interpreter_cache_revalidates_path_stamp_and_permissions(self):
        env=dict(self.env);env.pop('FPL_PY',None)
        self.assertEqual(self.invoke('guard',env=env).returncode,0)
        folder=self.root/'cache/fluxpoint/native-hooks'
        before=set(folder.glob('*.json'))
        env['PATH']=str(self.root/'empty-bin')+os.pathsep+env['PATH']
        self.assertEqual(self.invoke('guard',env=env).returncode,0)
        added=set(folder.glob('*.json'))-before
        self.assertTrue(added,'a PATH change must select a new interpreter cache entry')
        cache=next(iter(added));value=json.loads(cache.read_text(encoding='utf-8'));value['stamp']=[value['file'],0,0,0]
        cache.write_text(json.dumps(value))
        self.assertEqual(self.invoke('guard',env=env).returncode,0)
        self.assertNotEqual(json.loads(cache.read_text(encoding='utf-8'))['stamp'],value['stamp'])
        if os.name!='nt':
            cache.chmod(0o666)
            self.assertEqual(self.invoke('guard',env=env).returncode,0)
            self.assertEqual(cache.stat().st_mode & 0o077,0)
            folder.chmod(0o777)
            refused=self.invoke('guard',env=env)
            self.assertEqual(refused.returncode,2);self.assertIn('private',refused.stderr)
            folder.chmod(0o700)

    def test_auto_python_path_is_utf8_even_with_legacy_stdio_encoding(self):
        directory=self.root/'José';directory.mkdir()
        suffix='.exe' if os.name=='nt' else ''
        for name in ('python3','python'):
            shutil.copy2(sys.executable,directory/(name+suffix))
        if os.name=='nt':
            for dll in Path(sys.base_prefix).glob('*.dll'):shutil.copy2(dll,directory/dll.name)
        env=dict(self.env,PATH=str(directory),PYTHONHOME=sys.base_prefix,PYTHONIOENCODING='cp1252')
        env.pop('FPL_PY',None)
        result=subprocess.run([shutil.which('node'),str(ENTRY),'guard'],input='{}',cwd=self.root,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        values=[json.loads(p.read_text(encoding='utf-8'))['file'] for p in (self.root/'cache/fluxpoint/native-hooks').glob('*.json')]
        self.assertTrue(any(str(directory) in value for value in values))

    def fixture_plugin(self, worker):
        plugin=self.root/'plugin space $literal'
        (plugin/'scripts').mkdir(parents=True)
        shutil.copy2(ENTRY,plugin/'scripts/native-hook.mjs')
        (plugin/'scripts/secret-guard.py').write_text(worker)
        return plugin

    def orphan_worker(self, suspended=False):
        # The parent and orphan own only whatever stdio the launcher gives them.
        return """import json,os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr,creationflags=FLAGS)
Path(os.environ['FPL_TEST_PIDS']).write_text(json.dumps([os.getpid(),child.pid,os.getppid()]))
time.sleep(60)
""".replace('FLAGS','4' if suspended else '0')

    def process_alive(self, pid):
        if os.name=='nt':
            import ctypes
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32];api.OpenProcess.restype=ctypes.c_void_p
            api.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)];api.GetExitCodeProcess.restype=ctypes.c_int
            api.CloseHandle.argtypes=[ctypes.c_void_p];api.CloseHandle.restype=ctypes.c_int
            handle=api.OpenProcess(0x1000,False,pid)
            if not handle:
                error=ctypes.get_last_error()
                if error==87:return False
                raise OSError(error,'cannot observe controlled process liveness')
            try:
                code=ctypes.c_uint32()
                if not api.GetExitCodeProcess(handle,ctypes.byref(code)):raise OSError(ctypes.get_last_error())
                return code.value==259
            finally:api.CloseHandle(handle)
        try:
            os.kill(pid,0)
            stat=Path('/proc')/str(pid)/'stat'
            return not Path('/proc').is_dir() or stat.read_text(encoding='utf-8').rsplit(')',1)[-1].split()[0]!='Z'
        except (ProcessLookupError,FileNotFoundError):return False

    def kill_pid(self,pid):
        try:os.kill(pid,signal.SIGTERM if os.name=='nt' else signal.SIGKILL)
        except OSError:
            if self.process_alive(pid):raise

    def cleanup_pids(self, filename):
        if filename.exists():
            for pid in reversed(json.loads(filename.read_text(encoding='utf-8'))):self.kill_pid(pid)

    def await_file(self, filename, seconds=8):
        end=time.monotonic()+seconds
        while not filename.exists() and time.monotonic()<end: time.sleep(.025)
        self.assertTrue(filename.exists(),'worker did not create the controlled orphan')

    def test_host_pipes_close_after_launcher_kill_and_inherit_control_detects_leak(self):
        modes=[False,True] if os.name=='nt' else [False]
        for suspended in modes:
            for inherit in (False,True):
                with self.subTest(suspended=suspended,inherit=inherit):
                    pidfile=self.root/('pids-'+str(suspended)+'-'+str(inherit)+'.json')
                    plugin=self.fixture_plugin(self.orphan_worker(suspended)) if not (self.root/'plugin space $literal').exists() else self.root/'plugin space $literal'
                    (plugin/'scripts/secret-guard.py').write_text(self.orphan_worker(suspended))
                    entry=plugin/'scripts/native-hook.mjs'
                    command=['node',str(entry),'guard']
                    if inherit:
                        control=self.root/'inherit-control.mjs'
                        control.write_text("import {spawn} from 'node:child_process';spawn(process.env.FPL_PY,[process.argv[2]],{stdio:'inherit'});setInterval(()=>{},1000);")
                        command=['node',str(control),str(plugin/'scripts/secret-guard.py')]
                    env=dict(self.env,FPL_TEST_PIDS=str(pidfile))
                    proc=subprocess.Popen(command,cwd=self.root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    ended=threading.Event()
                    def collect():
                        proc.stdout.read();proc.stderr.read();ended.set()
                    reader=threading.Thread(target=collect,daemon=True);reader.start()
                    try:
                        proc.stdin.write(b'{}');proc.stdin.close()
                        self.await_file(pidfile)
                        proc.kill();proc.wait(timeout=3)
                        # Leave only the orphan alive; the suspended control must
                        # establish that orphan ownership, not its running parent.
                        worker=json.loads(pidfile.read_text(encoding='utf-8'))[0]
                        self.kill_pid(worker)
                        self.assertEqual(ended.wait(1.0),not inherit,'fixture must distinguish private and inherited hook pipes')
                    finally:
                        if proc.poll() is None:proc.kill();proc.wait(timeout=3)
                        self.cleanup_pids(pidfile);reader.join(timeout=4)
                        proc.stdout.close();proc.stderr.close()
                    self.assertTrue(ended.is_set())

    @unittest.skipUnless(LIVE,'real Claude smoke is opt-in (--live), run on Windows CI')
    def test_actual_claude_exec_form_and_forced_hook_timeout(self):
        claude=self.env['FPL_CLAUDE_BINARY']
        self.assertTrue(claude,'Claude2.1.312+ is required for --live')
        config=self.root/'claude-config';project=self.root/'project';project.mkdir()
        plugin=self.fixture_plugin("print('{}')")
        (plugin/'.claude-plugin').mkdir();(plugin/'hooks').mkdir()
        (plugin/'.claude-plugin/plugin.json').write_text(json.dumps({'name':'native-hook-fixture','version':'0.0.0','description':'Controlled local native hook integration fixture','license':'Apache-2.0'}))
        manifest=plugin/'hooks/hooks.json'
        marker=self.root/'literal $ marker.json'
        inspector=plugin/'inspect.mjs'
        inspector.write_text("import fs from 'node:fs';process.stdin.resume();process.stdin.on('end',()=>fs.writeFileSync(process.argv[2],JSON.stringify({ppid:process.ppid,args:process.argv.slice(2)})));")
        env=dict(self.env,CLAUDE_CONFIG_DIR=str(config),CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        def launch(hook,extra=None):
            manifest.write_text(json.dumps({'hooks':{'SessionStart':[{'hooks':[hook]}]}}))
            command=[claude,'--init-only','--plugin-dir',str(plugin),'--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}']
            process=subprocess.Popen(command,cwd=project,env={**env,**(extra or {})},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            return process
        first=launch({'type':'command','command':'node','args':['${CLAUDE_PLUGIN_ROOT}/inspect.mjs',str(marker)],'timeout':3})
        try:stdout,stderr=first.communicate(timeout=15)
        except subprocess.TimeoutExpired:first.kill();first.communicate();self.fail('real exec-form hook did not settle')
        self.assertEqual(first.returncode,0,stderr.decode(errors='replace'))
        observation=json.loads(marker.read_text(encoding='utf-8'));self.assertEqual(observation['ppid'],first.pid,'exec-form entry must have Claude as direct parent')
        self.assertEqual(observation['args'],[str(marker)])
        # Prime the version cache before a deliberately short runtime timeout.
        prime=self.invoke('guard',env=env);self.assertEqual(prime.returncode,0,prime.stderr)
        for suspended in ([False,True] if os.name=='nt' else [False]):
            with self.subTest(suspended=suspended):
                pidfile=self.root/('runtime-pids-'+str(suspended)+'.json')
                (plugin/'scripts/secret-guard.py').write_text(self.orphan_worker(suspended))
                start=time.monotonic()
                actual=launch({'type':'command','command':'node','args':['${CLAUDE_PLUGIN_ROOT}/scripts/native-hook.mjs','guard'],'timeout':3,'onFailure':'block'}, {'FPL_TEST_PIDS':str(pidfile)})
                try:
                    out,err=actual.communicate(timeout=30)
                    self.assertTrue(pidfile.exists(),'runtime timeout must occur after worker creates orphan')
                    self.assertFalse(self.process_alive(json.loads(pidfile.read_text(encoding='utf-8'))[2]),'real runtime timeout must terminate its native hook entry')
                    self.assertLess(time.monotonic()-start,25)
                    self.assertEqual(actual.returncode,0,err.decode(errors='replace'))
                finally:
                    if actual.poll() is None:actual.kill();actual.communicate(timeout=3)
                    self.cleanup_pids(pidfile)


if __name__ == '__main__': unittest.main()
