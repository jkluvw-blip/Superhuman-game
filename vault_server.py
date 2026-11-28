import os, json, secrets, hashlib, hmac, time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock
from urllib.parse import urlsplit
import psycopg
K=timezone(timedelta(hours=9)); lock=Lock()
def connect():
    return psycopg.connect(os.environ["DATABASE_URL"])
def setup():
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS players(name text PRIMARY KEY, salt text, pin text, token text UNIQUE)")
        c.execute("CREATE TABLE IF NOT EXISTS bids(day text, r int, name text, amount bigint, PRIMARY KEY(day,r,name))")
        c.execute("CREATE TABLE IF NOT EXISTS results(day text,r int,detail text,PRIMARY KEY(day,r))")
def clock():
    d=datetime.now(K); return d,d.strftime("%Y-%m-%d"),d.hour*2+d.minute//30
def state(c,day,r):
    bal={n:1000000 for (n,) in c.execute("SELECT name FROM players")}; hist=[]
    for i in range(18,min(r,48)):
        old=c.execute("SELECT detail FROM results WHERE day=%s AND r=%s",(day,i)).fetchone()
        if old: q=json.loads(old[0])
        else:
            bids=dict(c.execute("SELECT name,amount FROM bids WHERE day=%s AND r=%s",(day,i)))
            if not bids: continue
            levels=sorted(set(bids.values()),reverse=True)
            winners=sorted(n for n,v in bids.items() if len(levels)>1 and v==levels[1])
            pot=sum(bids.values()); payout={n:0 for n in bids}
            if winners:
                for j,n in enumerate(winners): payout[n]=pot//len(winners)+int(j<pot%len(winners))
            else: payout=bids.copy()
            q=dict(r=i,bids=bids,winners=winners,payout=payout,pot=pot)
            c.execute("INSERT INTO results VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(day,i,json.dumps(q,ensure_ascii=False)))
        for n,v in q["bids"].items(): bal[n]=bal.get(n,1000000)-v+q["payout"][n]
        hist.append(q)
    return bal,hist
def ranks(b): return sorted(b.items(),key=lambda x:(-x[1],x[0]))
def digest(pin,salt): return hashlib.scrypt(pin.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
class H(BaseHTTPRequestHandler):
    def send(self,data,status=200,html=False):
        b=data.encode() if html else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","text/html; charset=utf-8" if html else "application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self.path=urlsplit(self.path).path
        if self.path=="/": return self.send(PAGE,html=True)
        if self.path=="/health": return self.send({"ok":True})
        if self.path=="/state": return self.run({})
        self.send({"error":"Not found"},404)
    def do_POST(self):
        self.path=urlsplit(self.path).path
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=4096: return self.send({"error":"잘못된 요청"},400)
            p=json.loads(self.rfile.read(size))
            if not isinstance(p,dict): raise ValueError()
        except Exception: return self.send({"error":"잘못된 요청"},400)
        if self.path not in ("/join","/bid"): return self.send({"error":"Not found"},404)
        self.run(p)
    def run(self,p):
        try:
            with lock,connect() as c:
                d,day,r=clock()
                if self.path=="/join":
                    n=str(p.get("name","")).strip(); pin=str(p.get("pin","")); code=str(p.get("code",""))
                    if not hmac.compare_digest(code.strip().encode(),os.environ["ROOM_CODE"].encode()): return self.send({"error":"방 코드 오류"},403)
                    if not 1<=len(n)<=12 or not 6<=len(pin)<=64: return self.send({"error":"닉네임 1~12자, 비밀번호 6~64자"},400)
                    old=c.execute("SELECT salt,pin,token FROM players WHERE name=%s",(n,)).fetchone()
                    if old:
                        if not hmac.compare_digest(digest(pin,old[0]),old[1]): return self.send({"error":"비밀번호 오류"},403)
                        return self.send({"token":old[2]})
                    if c.execute("SELECT count(*) FROM players").fetchone()[0]>=8: return self.send({"error":"8명 정원 마감"},400)
                    salt=secrets.token_hex(16); tok=secrets.token_urlsafe(32)
                    c.execute("INSERT INTO players VALUES(%s,%s,%s,%s)",(n,salt,digest(pin,salt),tok)); c.commit()
                    return self.send({"token":tok})
                row=c.execute("SELECT name FROM players WHERE token=%s",(self.headers.get("X-Token",""),)).fetchone()
                if not row: return self.send({"error":"로그인 필요"},401)
                n=row[0]; bal,hist=state(c,day,r)
                if self.path=="/bid":
                    a=p.get("amount")
                    if p.get("day")!=day or p.get("r")!=r or r<18 or type(a)!=int or not 0<=a<=bal[n]: return self.send({"error":"입찰 금액 또는 마감 시간 오류. 새로고침하세요."},400)
                    if a: c.execute("INSERT INTO bids VALUES(%s,%s,%s,%s) ON CONFLICT(day,r,name) DO UPDATE SET amount=excluded.amount",(day,r,n,a))
                    else: c.execute("DELETE FROM bids WHERE day=%s AND r=%s AND name=%s",(day,r,n))
                own=c.execute("SELECT amount FROM bids WHERE day=%s AND r=%s AND name=%s",(day,r,n)).fetchone()
                count=c.execute("SELECT count(*) FROM bids WHERE day=%s AND r=%s",(day,r)).fetchone()[0]
                prev=(d-timedelta(days=1)).strftime("%Y-%m-%d"); final=ranks(state(c,prev,48)[0])
                end=d.replace(hour=9,minute=0,second=0,microsecond=0) if r<18 else d.replace(minute=d.minute//30*30,second=0,microsecond=0)+timedelta(minutes=30)
                c.commit()
                self.send(dict(name=n,day=day,r=r,end=int(end.timestamp()),active=r>=18,balance=bal[n],rank=ranks(bal),own=own[0] if own else 0,count=count,history=hist[::-1],final=final))
        except Exception as e:
            print(type(e).__name__,str(e),flush=True); self.send({"error":"서버 오류"},500)
PAGE="""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>비밀금고</title><style>
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#302719,#0b1015 55%);color:#eee9df;font-family:system-ui,sans-serif}main{max-width:850px;margin:auto;padding:24px 16px}header{letter-spacing:4px;color:#c9a96b;font-size:11px}h1{font-size:42px;margin:8px 0}small,.muted{color:#a7abb2;font-size:13px;line-height:1.6}section{background:#141a21;border:1px solid #383b3e;border-radius:18px;padding:20px;margin:14px 0;box-shadow:0 12px 30px #0004}h2{font-size:15px;margin:0 0 14px;color:#e0c18a}.hero{text-align:center;background:radial-gradient(circle at 50% 25%,#3c3629,#151b22 65%)}.vault{margin:15px auto;width:130px;height:130px;border-radius:50%;border:10px solid #817c6b;box-shadow:0 0 0 5px #343a40,inset 0 0 0 4px #24282d,0 0 35px #c5a36b33;background:repeating-conic-gradient(#585c5f 0 12deg,#42474b 12deg 45deg);display:grid;place-items:center}.vault:after{content:'✥';font-size:70px;color:#e0c18a;text-shadow:0 3px #111}#timer{font-size:44px;font-variant-numeric:tabular-nums;font-weight:800;color:#f1d59a}.money{font-size:30px;font-weight:800;color:#f1d59a}input,button{font:inherit;border-radius:10px;padding:13px;border:1px solid #41464a}input{background:#0c1117;color:white;width:100%;margin:6px 0}button{background:#c4a36b;color:#15120c;font-weight:800;cursor:pointer}button.alt{background:#252d36;color:#dedbd3}button:disabled{opacity:.4}.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1}.rank{display:flex;justify-content:space-between;gap:8px;padding:12px 0;border-bottom:1px solid #292f35}.me{color:#f1d59a}.tag{display:inline-block;border:1px solid #8c784f;color:#e3c68a;padding:4px 10px;border-radius:20px;font-size:12px}.msg{color:#f1c78a;min-height:20px}details{padding:12px 0;border-bottom:1px solid #30363b}summary{cursor:pointer}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:620px){.grid{display:block}h1{font-size:36px}}[hidden]{display:none!important}</style>
<main><header>THE SECRET VAULT / PRIVATE CLUB</header><h1>비밀금고</h1><small>최고액은 함정이다. 두 번째 금액이 금고를 연다.</small><section id="login"><h2>PRIVATE ROOM · 8인 전용</h2><input id="nick" placeholder="닉네임" maxlength="12"><input id="pin" type="password" placeholder="개인 비밀번호 (6자 이상)"><input id="code" type="password" placeholder="방 코드"><button onclick="join()">금고 입장</button><p class="msg" id="err"></p></section><div id="app" hidden><section class="hero"><span class="tag" id="round"></span><div class="vault"></div><small id="phase"></small><div id="timer">00:00</div><p id="count" class="muted"></p></section><div class="grid"><div><section><h2>나의 자산</h2><div class="money" id="balance"></div><p id="own" class="muted"></p><input id="amount" type="number" min="0" step="1" placeholder="비밀 입찰 금액"><div class="row"><button class="alt" onclick="preset(.1)">10%</button><button class="alt" onclick="preset(.25)">25%</button><button class="alt" onclick="preset(.5)">50%</button><button class="alt" onclick="preset(1)">ALL IN</button></div><p class="row"><button id="submit" onclick="bid()">비밀 입찰 확정</button><button class="alt" onclick="bid(0)">취소</button></p><p id="msg" class="msg"></p></section><section><h2>게임 규칙</h2><p class="muted">매일 오전 9시~자정, 30라운드. 시작 자산 100만 가상 코인. 최고액은 탈락, 두 번째로 높은 서로 다른 금액이 전체 판돈을 획득. 공동 승자는 균등 분배. 금액 종류가 하나뿐이면 전원 환불. 미참가는 자산 유지. 현금 충전·환전 없음.</p></section></div><div><section><h2>실시간 자산 순위</h2><div id="ranking"></div></section><section><h2>금고 개방 기록</h2><div id="history"></div></section><section><h2>어제 최종 순위</h2><div id="finalrank"></div></section></div></div><button class="alt" onclick="localStorage.removeItem('vaultToken');location.reload()">로그아웃</button></div></main><script>
let token=localStorage.getItem('vaultToken')||'',s;const $=x=>document.getElementById(x),fmt=x=>Number(x).toLocaleString('ko-KR')+' C',esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function api(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':token},body:data?JSON.stringify(data):undefined});return r.json()}async function join(){try{let x=await api('/join',{name:$('nick').value,pin:$('pin').value,code:$('code').value});if(x.error)throw Error(x.error);token=x.token;localStorage.setItem('vaultToken',token);refresh()}catch(e){$('err').textContent=e.message}}function preset(p){if(s)$('amount').value=Math.floor(s.balance*p)}async function bid(a){try{let x=await api('/bid',{amount:a??Number($('amount').value),day:s.day,r:s.r});if(x.error)throw Error(x.error);render(x);$('msg').textContent=a===0?'입찰 취소 완료':'비밀 입찰 저장 완료'}catch(e){$('msg').textContent=e.message}}function ranks(a){return a.map(([n,b],i)=>'<div class="rank '+(n===s.name?'me':'')+'"><span>'+ (i+1)+'. '+esc(n)+'</span><b>'+fmt(b)+'</b></div>').join('')}function render(x){let changed=!s||s.r!==x.r||s.history.length!==x.history.length;s=x;$('login').hidden=true;$('app').hidden=false;$('round').textContent=x.active?'ROUND '+(x.r-17)+' / 30':'경기 종료';$('phase').textContent=x.active?'금고 개방까지':'다음 경기 시작까지';$('count').textContent='입찰 완료 '+x.count+' / 8명 · 금액 비공개';$('balance').textContent=fmt(x.balance);$('own').textContent='현재 내 입찰: '+fmt(x.own);$('submit').disabled=!x.active;$('ranking').innerHTML=ranks(x.rank);$('finalrank').innerHTML=ranks(x.final);if(changed)$('history').innerHTML=x.history.map(h=>'<details><summary>R'+(h.r-17)+' · '+(h.winners.length?h.winners.map(esc).join(', ')+' 승리':'전원 환불')+' · '+fmt(h.pot)+'</summary>'+Object.entries(h.bids).sort((a,b)=>b[1]-a[1]).map(([n,v])=>'<div class="rank"><span>'+esc(n)+'</span><span>'+fmt(v)+' → '+fmt(h.payout[n])+'</span></div>').join('')+'</details>').join('')||'<p class="muted">아직 개방된 금고가 없습니다.</p>';tick()}async function refresh(){if(!token)return;try{let x=await api('/state');if(x.error){$('err').textContent=x.error;return}render(x)}catch(e){$('msg').textContent='서버 연결 확인 중'}}function tick(){if(!s)return;let t=Math.max(0,s.end-Math.floor(Date.now()/1000));$('timer').textContent=[Math.floor(t/3600),Math.floor(t/60)%60,t%60].map(n=>String(n).padStart(2,'0')).join(':')}setInterval(tick,250);setInterval(refresh,5000);refresh();</script></html>"""
if __name__=="__main__":
    setup()
    from threading import Thread
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    server=ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8080"))),H)
    Thread(target=server.serve_forever,daemon=True).start()
    base="http://127.0.0.1:"+str(server.server_port)
    def probe(path, data=None, token=""):
        request=Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={"Content-Type":"application/json","X-Token":token})
        try:
            with urlopen(request,timeout=10) as response:
                return response.status,response.read()
        except HTTPError as e:
            return e.code,e.read()
    try:
        assert probe("/?utm_source=chatgpt.com")[0]==200
        code=os.environ["ROOM_CODE"]
        assert probe("/join",{"name":"","pin":"","code":code})[0]==400
        assert probe("/join",{"name":"","pin":"","code":code+"!"})[0]==403
        with connect() as c:
            total=c.execute("SELECT count(*) FROM players").fetchone()[0]
        if total<8:
            name="T"+secrets.token_hex(5); pin=secrets.token_hex(12)
            try:
                status,body=probe("/join",{"name":name,"pin":pin,"code":code})
                assert status==200
                token=json.loads(body)["token"]
                assert probe("/state",token=token)[0]==200
                status,body=probe("/join",{"name":name,"pin":pin,"code":code})
                assert status==200 and json.loads(body)["token"]==token
                print("VAULT SELFTEST PASS: public path, room code, account signup, login and balance API",flush=True)
            finally:
                with connect() as c:
                    c.execute("DELETE FROM players WHERE name=%s",(name,))
        else:
            print("VAULT SELFTEST PASS: public path and room code; signup skipped because room is full",flush=True)
    except Exception as e:
        print("VAULT SELFTEST FAILED:",type(e).__name__,str(e),flush=True)
    from threading import Event
    Event().wait()
