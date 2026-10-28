import os, json, re, shlex, subprocess, tempfile, threading, queue, time, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codex_backend import AppServer, stdin_approval_details
from pet_panel import _safe_approval_text
log=[]
sample=os.environ.get('PET_TEST_SAMPLE', 'special')
if sample=='special': payload="A'B\"C\\D\n\t\x1b[31mX\u202e中文"
elif sample=='long': payload='A'*300000
else: payload='printf LOCAL_TEST_ONLY\n'
stdin_requests=0
long_rejected=False
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  global long_rejected
  body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
  inputs=body.get('input',[])
  outs=[i for i in inputs if i.get('type')=='function_call_output']
  n=len(outs)
  print('stub request',n, 'tools:', [t.get('name',t.get('type')) for t in body.get('tools',[])],flush=True)
  if n==0:
   item={'type':'function_call','call_id':'call_exec','name':'exec_command','arguments':json.dumps({'cmd':'/bin/cat','tty':True,'yield_time_ms':1000,'sandbox_permissions':'require_escalated','justification':'Isolated local test terminal'})}
  elif n in (1,2):
   m=re.search(r'(?:session ID|session_id|Session ID|session id)\s*[:=]?\s*(\d+)',str(outs[0].get('output')),re.I)
   if not m: item={'type':'message','role':'assistant','id':'message','content':[{'type':'output_text','text':'No session id'}]}
   else:
    item={'type':'function_call','call_id':'call_stdin'+str(n),'name':'write_stdin','arguments':json.dumps({'session_id':int(m.group(1)),'chars':payload+('SECOND' if n==2 else ''),'yield_time_ms':1000})}
  else:
   if sample == 'long':
    long_rejected = 'terminal input and permission details are too large' in str(outs[-1].get('output'))
   print('last output summary:',str(outs[-1].get('output'))[:160],flush=True)
   item={'type':'message','role':'assistant','id':'message','content':[{'type':'output_text','text':'Done'}]}
  events=[{'type':'response.created','response':{'id':'resp'+str(n)}},{'type':'response.output_item.done','item':item},{'type':'response.completed','response':{'id':'resp'+str(n),'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  for e in events:self.wfile.write(('data: '+json.dumps(e)+'\n\n').encode())
  self.wfile.flush()
http=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix='pet-stdin-isolate-') as d:
 env={'PATH':os.environ['PATH'],'HOME':d,'CODEX_HOME':d,'OPENAI_API_KEY':'fake-test-key','NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost','LANG':'C.UTF-8'}
 args=['codex','app-server','--listen','stdio://','-c','model_provider="teststub"','-c','model_providers.teststub.name="teststub"','-c','model_providers.teststub.env_key="OPENAI_API_KEY"','-c',f'model_providers.teststub.base_url="http://127.0.0.1:{http.server_port}/v1"','-c','model_providers.teststub.wire_api="responses"','-c','model_providers.teststub.supports_websockets=false']
 p=subprocess.Popen(args,env=env,cwd=d,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
 q=queue.Queue()
 def reader():
  for line in p.stdout:
   try:q.put(json.loads(line))
   except ValueError: pass
  q.put(None)
 threading.Thread(target=reader,daemon=True).start()
 def send(x):p.stdin.write(json.dumps(x)+'\n');p.stdin.flush()
 bridge=object.__new__(AppServer)
 bridge.mode='task'; bridge.pending_approvals={}; bridge.process=p
 bridge.write=send; bridge.emit=lambda *args: None
 def take(i):
  while True:
   e=q.get(timeout=20)
   if e is None:raise Exception('exited '+p.stderr.read())
   if e.get('id')==i and 'method' not in e:
    if 'error' in e:raise Exception(e['error'])
    return e['result']
 try:
  send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'stdin-probe','version':'1'}}});take(1)
  send({'method':'initialized','params':{}})
  send({'id':2,'method':'thread/start','params':{'cwd':d,'model':'gpt-5','approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':'workspace-write'}});tid=take(2)['thread']['id']
  send({'id':3,'method':'turn/start','params':{'threadId':tid,'input':[{'type':'text','text':'Synthetic test'}]}});take(3)
  deadline=time.monotonic()+30
  completed=False
  while time.monotonic()<deadline:
   try:e=q.get(timeout=max(.1,deadline-time.monotonic()))
   except queue.Empty:break
   if e is None:break
   method=e.get('method','')
   if method=='item/commandExecution/terminalInteraction': print('terminal interaction', 'same-input', e['params']['stdin']==payload, 'len',len(e['params']['stdin']),flush=True)
   if method=='turn/completed': print('turn completed',e['params']['turn']['status'],flush=True)
   if method=='item/commandExecution/requestApproval':
    params=e['params']; kind=params.get('kind'); command=params.get('command','')
    if kind=='writeStdin':
     stdin_requests+=1
     try: split=shlex.split(command); matches=(len(split)==4 and split[0]=='write_stdin' and split[1]=='--session-id' and split[3]==payload+('SECOND' if stdin_requests==2 else ''))
     except ValueError: matches=False
     assert (stdin_approval_details(params) is None) == (sample == 'long')
     display = _safe_approval_text(params)
     assert '\u202e' not in display and '\u001b' not in display
     assert matches
     print('stdin approval',stdin_requests,'command length',len(command),'input length',len(payload),'shlex roundtrip',matches,'itemId',params.get('itemId'),'approvalId',params.get('approvalId'),'decisions',params.get('availableDecisions'),flush=True)
     decision='accept' if stdin_requests==1 and sample != 'long' else 'cancel'
    else: decision='accept'
    if kind=='writeStdin':
     bridge.handle(e)
     assert bridge._stdin_protocol_verified()
     if sample == 'long':
      try: bridge.approve(e['id'], 'accept')
      except ValueError: pass
      else: raise AssertionError('oversized input approved')
     bridge.approve(e['id'], decision)
    else:
     send({'id':e['id'],'result':{'decision':decision}})
   if method=='turn/completed':
    if sample == 'long':
     assert stdin_requests == 0 and long_rejected and e['params']['turn']['status'] == 'completed'
    else:
     assert stdin_requests == 2 and e['params']['turn']['status'] == 'interrupted'
    completed=True
    break
  assert completed, 'No completed approval sequence before timeout'
 finally:
  p.terminate()
  try:p.wait(timeout=3)
  except subprocess.TimeoutExpired:p.kill();p.wait()
  p.stdin.close();p.stdout.close();p.stderr.close();http.shutdown();http.server_close()
