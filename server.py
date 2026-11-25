import asyncio, json, os, secrets, string, time
from pathlib import Path
from aiohttp import web, WSMsgType
rooms={}
colors=['#f34b43','#45b9f5','#52dc95','#e7b74c']
def player(i,name): return dict(i=i,name=name,col=colors[i],p=0,v=0,last=-1,phase=0,hit=0,time=None,last_at=0)
def state(r):
 return dict(type='state',code=r['code'],host=r['host'],phase=r['phase'],t=round(r['t'],3),players=[{k:v for k,v in p.items() if k not in ('last','last_at')} for p in r['players'].values()],order=r['order'])
async def send(ws,data):
 try: await ws.send_json(data)
 except (ConnectionError,RuntimeError): pass
async def broadcast(r): await asyncio.gather(*(send(w,state(r)) for w in list(r['sockets'].values())))
async def socket(req):
 ws=web.WebSocketResponse(heartbeat=20,max_msg_size=2048);await ws.prepare(req);r=None;i=None
 try:
  async for msg in ws:
   if msg.type!=WSMsgType.TEXT: continue
   try: d=json.loads(msg.data)
   except ValueError: continue
   if not isinstance(d,dict): continue
   cmd=d.get('type');now=time.monotonic()
   if cmd in ('create','join') and r is None:
    if cmd=='create':
     code=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6))
     while code in rooms: code=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6))
     r=dict(code=code,host=0,phase='lobby',players={},sockets={},t=0,order=[],go=0,prev=now);rooms[code]=r
    else:
     r=rooms.get(str(d.get('code','')).upper())
     if not r or r['phase'] not in ('lobby','finished') or len(r['players'])>=4:
      r=None;await send(ws,dict(type='error',message='방이 없거나 가득 찼거나 경기 중입니다.'));continue
    i=next(j for j in range(4) if j not in r['players']);r['players'][i]=player(i,str(d.get('name','')).strip()[:16] or f'{i+1}P');r['sockets'][i]=ws
    await send(ws,dict(type='joined',id=i,code=r['code']));await broadcast(r)
   elif r is not None and cmd=='start' and i==r['host'] and r['phase'] in ('lobby','finished') and len(r['players'])==4:
    r['players']={j:player(j,p['name']) for j,p in r['players'].items()};r.update(phase='countdown',go=now+3,prev=now,t=-3,order=[]);await broadcast(r)
   elif r is not None and cmd=='hit' and r['phase']=='race':
    p=r['players'][i];j=d.get('key')
    if type(j) is int and j in (0,1) and j!=p['last'] and p['time'] is None and now-p['last_at']>=.045:
     p['last']=j;p['last_at']=now;p['v']=min(99.8,p['v']+7.2*(1-p['v']/100)**1.55);p['hit']=.15;p['phase']+=.25
 finally:
  if r is not None and i is not None:
   r['sockets'].pop(i,None);r['players'].pop(i,None)
   if not r['players']: rooms.pop(r['code'],None)
   else:
    if i==r['host']:r['host']=min(r['players'])
    if r['phase'] in ('race','countdown'):r.update(phase='lobby',t=0,order=[])
    await broadcast(r)
 return ws
async def tick():
 while True:
  await asyncio.sleep(.05);now=time.monotonic()
  for r in list(rooms.values()):
   if r['phase'] not in ('countdown','race'):continue
   r['t']=now-r['go'];dt=max(0,min(.15,now-max(r['prev'],r['go'])));r['prev']=now
   if r['t']>=0:
    r['phase']='race';cross=[]
    for p in r['players'].values():
     if p['time'] is not None:continue
     p['hit']=max(0,p['hit']-dt);p['v']=max(0,p['v']-dt*(4+p['v']*.045));old=p['p'];p['p']=min(1500,old+p['v']*dt);p['phase']+=dt*p['v']*.24
     if p['p']>=1500:p['time']=max(0,r['t']-dt+(1500-old)/max(.001,p['v']));cross.append(p)
    r['order'].extend(p['i'] for p in sorted(cross,key=lambda p:p['time']))
    if len(r['order'])==4 or r['t']>=60:r['phase']='finished'
   await broadcast(r)
async def lifecycle(app):
 task=asyncio.create_task(tick());yield;task.cancel();await asyncio.gather(task,return_exceptions=True)
async def index(req):return web.FileResponse(Path(__file__).with_name('index.html'))
app=web.Application();app.router.add_get('/',index);app.router.add_get('/ws',socket);app.cleanup_ctx.append(lifecycle)
if __name__=='__main__':web.run_app(app,host='0.0.0.0',port=int(os.environ.get('PORT','8080')))
