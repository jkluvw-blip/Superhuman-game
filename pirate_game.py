import os, json, secrets, hashlib, hmac, random
from datetime import datetime, timedelta, timezone
from threading import Lock
import psycopg
K=timezone(timedelta(hours=9))
lock=Lock()
MODES={
 "trade":("무역선","안정적인 항로",85,1.30,.45),
 "treasure":("보물선","해저 보물 탐사",40,2.8,.10),
 "ghost":("유령선","심연의 전설",15,6.4,.05),
 "raid":("해적선","친구의 화물 약탈",60,1,.20),
 "guard":("호위선","안전한 항해와 반격",100,1.06,1)}
def connect(): return psycopg.connect(os.environ["DATABASE_URL"])
def setup():
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS pirate_players(name text PRIMARY KEY,salt text,pin text,token text UNIQUE)")
        c.execute("CREATE TABLE IF NOT EXISTS pirate_seed(id int PRIMARY KEY,secret text NOT NULL)")
        c.execute("INSERT INTO pirate_seed VALUES(1,%s) ON CONFLICT DO NOTHING",(secrets.token_hex(32),))
        c.execute("CREATE TABLE IF NOT EXISTS pirate_orders(day text,r int,name text,mode text,stake bigint,target text,upgrade boolean,PRIMARY KEY(day,r,name))")
def clock():
    d=datetime.now(K);return d,d.strftime("%Y-%m-%d"),d.hour*2+d.minute//30
def gen(seed,*x): return random.Random(int.from_bytes(hmac.digest(seed.encode(),"|".join(map(str,x)).encode(),"sha256"),"big"))
def weather(seed,day,r):
    z=gen(seed,day,r,"weather")
    name,td,ed,rd=z.choice([("잔잔한 바다",5,0,-5),("무역풍",8,-4,0),("보물 지도 발견",0,8,0),("해적의 안개",-5,0,10),("폭풍 경보",-10,-5,5),("황금 조류",0,5,-5)])
    if r==47: return ("마지막 항해 · 황금 조류",0,8,0)
    return name,td,ed,rd
def chance(mode,level,w,target_mode=None):
    _,td,ed,rd=w
    if mode=="trade": return max(5,min(98,85+td+level*2))
    if mode=="treasure": return max(5,min(90,40+ed+level*3))
    if mode=="ghost": return max(5,min(70,15+ed//2+level*2))
    if mode=="raid": return max(5,min(90,(20 if target_mode=="guard" else 60)+rd+level*3))
    return 100
def upgrade_cost(level): return level*80000
def settle(seed,day,r,balances,levels,orders):
    w=weather(seed,day,r); out={}; gross={}; gain={n:0 for n in orders}
    for n,q in orders.items():
        mode,stake,target,upgrade=q
        cost=upgrade_cost(levels[n]) if upgrade else 0
        if upgrade: levels[n]+=1
        balances[n]-=stake+cost
        rec=dict(name=n,mode=mode,stake=stake,target=target,upgrade=upgrade,cost=cost,level=levels[n],loot=0,lost=0)
        out[n]=rec
        if mode=="raid": continue
        p=chance(mode,levels[n]-1,w); z=gen(seed,day,r,n,"sail")
        ok=z.randrange(100)<p
        mult=MODES[mode][3 if ok else 4]
        if ok and mode!="guard": mult+=.025*(levels[n]-1)
        gross[n]=int(stake*mult)
        rec.update(ok=ok,chance=p,event=("항해 성공" if ok else "항해 실패"))
    for n in sorted(orders,key=lambda n:gen(seed,day,r,n,"raid-order").random()):
        mode,stake,target,upgrade=orders[n]
        if mode!="raid": continue
        rec=out[n]
        if target not in gross:
            gain[n]=stake*65//100; rec.update(ok=False,chance=0,event="표적이 출항하지 않음 · 65% 회수"); continue
        tm=orders[target][0]; p=chance("raid",levels[n]-1,w,tm)
        if tm=="guard": p=max(5,p-3*(levels[target]-1))
        ok=gen(seed,day,r,n,"raid").randrange(100)<p
        if ok:
            loot=min(stake*2,gross[target]*45//100)
            gross[target]-=loot; gain[n]=stake+loot; rec["loot"]=loot;out[target]["lost"]+=loot
            rec.update(ok=True,chance=p,event="약탈 성공")
        elif tm=="guard":
            bounty=stake*50//100; gross[target]+=bounty; gain[n]=stake*20//100;out[target]["loot"]+=bounty
            rec.update(ok=False,chance=p,event="호위선 반격")
        else:
            gain[n]=stake*20//100;rec.update(ok=False,chance=p,event="약탈 실패")
    for n,rec in out.items():
        pay=gain[n] if rec["mode"]=="raid" else gross[n]
        balances[n]+=pay;rec["payout"]=pay;rec["delta"]=pay-rec["stake"]-rec["cost"];rec["balance"]=balances[n]
    return dict(r=r,weather=w[0],results=sorted(out.values(),key=lambda v:(-v["delta"],v["name"])))
def state(c,day,r):
    seed=c.execute("SELECT secret FROM pirate_seed WHERE id=1").fetchone()[0]
    balances={n:1000000 for (n,) in c.execute("SELECT name FROM pirate_players")}
    levels={n:1 for n in balances}; by={};history=[]
    for i,n,mode,stake,target,up in c.execute("SELECT r,name,mode,stake,target,upgrade FROM pirate_orders WHERE day=%s",(day,)):
        if n in balances: by.setdefault(i,{})[n]=(mode,stake,target,up)
    for i in range(18,min(r,48)):
        if by.get(i): history.append(settle(seed,day,i,balances,levels,by[i]))
    rank=sorted([dict(name=n,balance=b,level=levels[n]) for n,b in balances.items()],key=lambda q:(-q["balance"],q["name"]))
    return seed,balances,levels,by.get(r,{}),history,rank
def digest(pin,salt): return hashlib.scrypt(pin.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
def handle(h,p=None):
    path=h.path.rstrip("/")
    if p is None and path=="/pirates": return h.send(PAGE,html=True)
    if path not in ("/pirates/state","/pirates/join","/pirates/order"): return h.send({"error":"Not found"},404)
    if p is None and path!="/pirates/state": return h.send({"error":"Method not allowed"},405)
    p=p or {}
    try:
        with lock,connect() as c:
            d,day,r=clock()
            if path=="/pirates/join":
                name=str(p.get("name","")).strip(); pin=str(p.get("pin","")); code=str(p.get("code","")).strip()
                if not hmac.compare_digest(code.encode(),os.environ["ROOM_CODE"].encode()): return h.send({"error":"입장 코드 오류"},403)
                if not 1<=len(name)<=12 or not 6<=len(pin)<=64: return h.send({"error":"닉네임 1~12자 / 비밀번호 6자 이상"},400)
                old=c.execute("SELECT salt,pin,token FROM pirate_players WHERE name=%s",(name,)).fetchone()
                if old:
                    if not hmac.compare_digest(digest(pin,old[0]),old[1]):return h.send({"error":"비밀번호 오류"},403)
                    return h.send({"token":old[2]})
                if c.execute("SELECT count(*) FROM pirate_players").fetchone()[0]>=8:return h.send({"error":"8인 정원 마감"},400)
                salt=secrets.token_hex(16); token=secrets.token_urlsafe(32)
                c.execute("INSERT INTO pirate_players VALUES(%s,%s,%s,%s)",(name,salt,digest(pin,salt),token));c.commit()
                return h.send({"token":token})
            row=c.execute("SELECT name FROM pirate_players WHERE token=%s",(h.headers.get("X-Token",""),)).fetchone()
            if not row:return h.send({"error":"로그인 필요"},401)
            n=row[0]; seed,b,l,orders,hist,rank=state(c,day,r)
            if path=="/pirates/order":
                if r<18 or p.get("day")!=day or p.get("r")!=r:return h.send({"error":"라운드 마감 또는 휴항 중"},400)
                mode=p.get("mode"); stake=p.get("stake"); target=p.get("target","");up=p.get("upgrade",False)
                if mode=="cancel":
                    c.execute("DELETE FROM pirate_orders WHERE day=%s AND r=%s AND name=%s",(day,r,n))
                else:
                    if not isinstance(mode,str) or mode not in MODES or type(stake)!=int or type(up)!=bool:return h.send({"error":"항해 설정 오류"},400)
                    cost=upgrade_cost(l[n]) if up else 0
                    if stake<1000 or stake+cost>b[n] or (up and l[n]>=5):return h.send({"error":"최소 1,000 C / 자산 또는 강화 한도 확인"},400)
                    if mode=="raid" and (not isinstance(target,str) or target==n or target not in b):return h.send({"error":"약탈할 친구를 선택하세요."},400)
                    if mode!="raid":target=""
                    c.execute("INSERT INTO pirate_orders VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(day,r,name) DO UPDATE SET mode=excluded.mode,stake=excluded.stake,target=excluded.target,upgrade=excluded.upgrade",(day,r,n,mode,stake,target,up))
                seed,b,l,orders,hist,rank=state(c,day,r)
            w=weather(seed,day,r); prev=(d-timedelta(days=1)).strftime("%Y-%m-%d")
            final=state(c,prev,48)[-1]
            end=d.replace(hour=9,minute=0,second=0,microsecond=0) if r<18 else d.replace(minute=d.minute//30*30,second=0,microsecond=0)+timedelta(minutes=30)
            c.commit()
            h.send(dict(version="pirates-v1",name=n,day=day,r=r,end=int(end.timestamp()),active=r>=18,balance=b[n],level=l[n],upgradeCost=upgrade_cost(l[n]),rank=rank,final=final,history=hist[::-1],own=orders.get(n),ready=len(orders),weather=w[0],chances={k:chance(k,l[n]-1,w) for k in MODES},guardRaid=chance("raid",l[n]-1,w,"guard")))
    except Exception as e:
        print("PIRATE ERROR",type(e).__name__,str(e),flush=True);h.send({"error":"서버 오류"},500)
def selftest():
    b={"a":1000000,"b":1000000};l={"a":1,"b":1}
    q=settle("test","2026-01-01",18,b,l,{"a":("guard",10000,"",True),"b":("raid",10000,"a",False)})
    assert len(q["results"])==2 and l["a"]==2 and min(b.values())>=0
    with connect() as c:
        assert c.execute("SELECT count(*) FROM pirate_seed").fetchone()[0]==1
    print("PIRATE ENGINE SELFTEST PASS: database, settlement, upgrade, raid",flush=True)

PAGE="<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>해적들의 전쟁</title><style>\n:root{--gold:#f5c677;--sea:#65d1c4;--muted:#96aab6;--line:#294654}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 60% 0,#234756,#071922 55%);color:#edf4f4;font:15px system-ui,sans-serif;min-height:100vh}main{max-width:960px;margin:auto;padding:18px 12px 65px}header{display:flex;align-items:center;justify-content:space-between}h1{font-size:clamp(28px,6vw,42px);margin:4px 0;letter-spacing:-1px}h2{font-size:18px;margin:0 0 13px}small,.muted{color:var(--muted);line-height:1.6}.label{font-size:10px;font-weight:800;letter-spacing:3px;color:var(--gold)}.pill{border:1px solid #9b7547;color:var(--gold);font-size:12px;border-radius:20px;padding:6px 10px;white-space:nowrap}.panel{border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0;background:linear-gradient(150deg,#183340,#10232c);box-shadow:0 8px 24px #0005}.hero{position:relative;overflow:hidden;min-height:230px;background:linear-gradient(#142b3a,#2e6170 60%,#113644 61%);padding:20px}.hero svg{position:absolute;inset:0;width:100%;height:100%;opacity:.8;pointer-events:none}.hero .top,.hero .bottom{position:relative;display:flex;justify-content:space-between;gap:12px}.hero .bottom{margin-top:95px;align-items:end}.timer{font-size:32px;font-weight:850;font-variant-numeric:tabular-nums;color:#fff;text-shadow:0 2px 8px #000}.money{font-size:26px;font-weight:850;color:var(--gold)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.ship{border:1px solid #3d5d66;border-radius:12px;background:#102731;text-align:left;color:white;padding:12px;cursor:pointer;min-height:102px}.ship.on{border-color:var(--gold);box-shadow:inset 0 0 0 1px var(--gold);background:#273b39}.ship span{display:block;font-size:12px;color:var(--muted);margin-top:4px}.ship b{font-size:17px}.ship em{font-style:normal;color:var(--sea);font-size:13px;display:block;margin-top:5px}.ship:last-child{grid-column:1/-1}button,input,select{font:inherit;border-radius:9px;border:1px solid #3c5a64;padding:11px}input,select{color:white;background:#091e28;width:100%;margin:5px 0}button{background:var(--gold);color:#22190c;font-weight:800;cursor:pointer}button.alt{background:#254451;color:#e0edef}button:disabled{opacity:.45;cursor:default}.row{display:flex;gap:7px;align-items:center}.row>*{flex:1;min-width:0}.tabs{display:flex;gap:6px;position:sticky;top:0;z-index:3;background:#0b202a;padding:8px 0}.tabs button{flex:1;background:#1b3946;color:#b8cad2;padding:10px 3px;font-size:13px}.tabs button.on{background:var(--gold);color:#1e1c14}.rank{display:grid;grid-template-columns:32px 1fr auto;align-items:center;gap:8px;border-bottom:1px solid var(--line);padding:12px 0}.rank strong{color:var(--gold)}.me{color:var(--sea)}.plus{color:#73dfb3}.minus{color:#ff9a87}.msg{font-size:13px;color:var(--gold);min-height:18px}.history{border-bottom:1px solid var(--line);padding:12px 0}summary{cursor:pointer}.upgrade{display:flex;gap:10px;align-items:center;padding:12px;border:1px dashed #92754b;border-radius:10px;margin:10px 0}.upgrade input{width:20px;margin:0;accent-color:#dfbd83}.empty{text-align:center;padding:20px;color:var(--muted)}[hidden]{display:none!important}\n</style><main><header><div><div class=\"label\">PIRATES / THE GOLDEN TIDE</div><h1>해적들의 전쟁</h1><small>황금의 바다에서 최후의 부호가 되어라.</small></div><span class=\"pill\">8인 항해단</span></header><section id=\"login\" class=\"panel\"><h2>항구 입장</h2><input id=\"nick\" maxlength=\"12\" placeholder=\"선장 닉네임\"><input id=\"pin\" type=\"password\" placeholder=\"개인 비밀번호 6자 이상\"><input id=\"code\" type=\"password\" placeholder=\"입장 코드\"><button onclick=\"join()\">출항 준비</button><p class=\"msg\" id=\"err\"></p></section><div id=\"app\" hidden><section class=\"panel hero\"><svg viewBox=\"0 0 600 240\" preserveAspectRatio=\"xMidYMid slice\"><defs><linearGradient id=\"sky\" x2=\"0\" y2=\"1\"><stop stop-color=\"#183743\"/><stop offset=\"1\" stop-color=\"#bd9771\"/></linearGradient></defs><rect width=\"600\" height=\"145\" fill=\"url(#sky)\"/><circle cx=\"435\" cy=\"70\" r=\"38\" fill=\"#e1c18a\" opacity=\".6\"/><path d=\"M0 145Q75 130 150 145T300 145T450 145T600 145V240H0\" fill=\"#173c4d\"/><path d=\"M0 185Q75 170 150 185T300 185T450 185T600 185M0 217Q75 202 150 217T300 217T450 217T600 217\" fill=\"none\" stroke=\"#82b3b9\" stroke-width=\"2\" opacity=\".35\"/><g transform=\"translate(215 35)\"><path d=\"M0 120h180l-32 33H38Z\" fill=\"#38291e\" stroke=\"#c19b62\" stroke-width=\"2\"/><path d=\"M90 0v122M40 38v83M140 45v75\" stroke=\"#29221b\" stroke-width=\"5\"/><path d=\"M88 9Q26 38 32 85h56ZM94 17q61 17 64 69H94ZM38 43 8 97h30ZM142 48l30 50h-30Z\" fill=\"#ded1ac\" stroke=\"#705d43\" stroke-width=\"2\"/><path d=\"M90 0l33 8-33 12Z\" fill=\"#7d3431\"/><circle cx=\"90\" cy=\"51\" r=\"9\" fill=\"#b18e61\"/></g></svg><div class=\"top\"><div><div class=\"label\" id=\"round\"></div><div class=\"timer\" id=\"timer\"></div><small id=\"phase\"></small></div><span class=\"pill\" id=\"ready\"></span></div><div class=\"bottom\"><div><small>선장 자산</small><div class=\"money\" id=\"balance\"></div></div><div style=\"text-align:right\"><small>선박 강화</small><div class=\"money\" id=\"level\"></div></div></div></section><section class=\"panel\"><div class=\"label\">항해 소식</div><h2 id=\"weather\" style=\"margin:6px 0\"></h2><small>날씨에 따라 성공 확률이 변해. 다른 선장의 계획은 마감까지 비밀.</small></section><nav class=\"tabs\"><button id=\"tplan\" onclick=\"tab('plan')\">항해 예약</button><button id=\"trank\" onclick=\"tab('rank')\">부자 순위</button><button id=\"thistory\" onclick=\"tab('history')\">항해 기록</button></nav><p id=\"msg\" class=\"msg\"></p><section id=\"plan\" class=\"panel\"><h2>이번 항해 선택</h2><div class=\"grid\" id=\"ships\"></div><div id=\"targetbox\" hidden><small>약탈 목표</small><select id=\"target\"></select></div><label class=\"upgrade\"><input type=\"checkbox\" id=\"upgrade\" onchange=\"limits()\"><span><b>선박 강화</b><br><small id=\"updesc\"></small></span></label><small>투자할 가상 코인</small><input id=\"stake\" type=\"number\" min=\"1000\" step=\"1000\" placeholder=\"최소 1,000\"><div class=\"row\"><button class=\"alt\" onclick=\"preset(.1)\">10%</button><button class=\"alt\" onclick=\"preset(.3)\">30%</button><button class=\"alt\" onclick=\"preset(.5)\">50%</button><button class=\"alt\" onclick=\"preset(1)\">최대</button></div><p id=\"saved\" class=\"muted\"></p><div class=\"row\"><button id=\"confirm\" onclick=\"order()\">항해 예약</button><button class=\"alt\" onclick=\"order(true)\">취소</button></div></section><section id=\"rank\" class=\"panel\" hidden><h2>오늘의 선장 순위</h2><div id=\"ranking\"></div><h2 style=\"margin-top:22px\">어제의 우승자</h2><div id=\"finalrank\"></div></section><section id=\"history\" class=\"panel\" hidden><h2>항해 결과 공개</h2><div id=\"records\"></div></section><section class=\"panel\"><h2>항해 규칙</h2><p class=\"muted\">매일 09:00~24:00 · 30분 간격 · 시작 자산 100만 C. 무역 성공 1.30배, 실패 0.45배. 보물 성공 2.8배, 실패 0.1배. 유령선 성공 6.4배, 실패 0.05배. 호위는 1.06배와 반격 보상. 해적은 표적의 화물 최대 45%, 투자금의 2배까지 약탈하고 성공 시 투자금도 회수. 실패 시 20% 회수, 표적 미출항·해적선이면 65% 회수. 강화는 다음 항해부터가 아니라 예약한 라운드에 적용되며 최대 5레벨. 강화 비용은 소모됨. 자정 최종 순위, 다음 날 초기화. 현금 충전·환전 없음.</p><button class=\"alt\" onclick=\"localStorage.removeItem('pirateToken');location.reload()\">로그아웃</button></section></div></main><script>\nlet token=localStorage.getItem('pirateToken')||'',s,mode='trade',view='plan',busy=false,epoch=0;const $=x=>document.getElementById(x),fmt=x=>Number(x).toLocaleString('ko-KR')+' C',esc=x=>String(x).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));const modes={trade:['⛵','무역선','안정적인 수익','성공 1.30배'],treasure:['💎','보물선','해저 보물 탐사','성공 2.8배'],ghost:['🌑','유령선','심연의 전설','성공 6.4배'],raid:['🏴‍☠️','해적선','친구 화물 약탈','표적에 따라 확률 변동'],guard:['🛡️','호위선','안전 항해 및 반격','기본 1.06배']};\nasync function api(path,data){let r=await fetch('/pirates'+path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':token},body:data?JSON.stringify(data):undefined});return r.json()}\nasync function join(){try{let x=await api('/join',{name:$('nick').value,pin:$('pin').value,code:$('code').value});if(x.error)throw Error(x.error);token=x.token;localStorage.setItem('pirateToken',token);refresh()}catch(e){$('err').textContent=e.message}}\nfunction tab(t){view=t;for(let k of ['plan','rank','history']){$(k).hidden=k!==t;$('t'+k).classList.toggle('on',k===t)}}\nfunction limits(){if(!s)return;$('updesc').textContent=s.level>=5?'최대 레벨':fmt(s.upgradeCost)+' · 성공 확률 보너스';$('upgrade').disabled=s.level>=5||!s.active;$('stake').max=Math.max(0,s.balance-($('upgrade').checked?s.upgradeCost:0));drawShips()}\nfunction preset(p){$('stake').value=Math.floor(Number($('stake').max)*p)}\nfunction choose(k){mode=k;drawShips()}\nfunction drawShips(){if(!s)return;let boost=$('upgrade').checked?({trade:2,treasure:3,ghost:2,raid:3,guard:0}):{};$('ships').innerHTML=Object.entries(modes).map(([k,a])=>'<button class=\"ship '+(mode===k?'on':'')+'\" onclick=\"choose(\\''+k+'\\')\"><b>'+a[0]+' '+a[1]+'</b><span>'+a[2]+' · '+a[3]+'</span><em>'+Math.min(k==='guard'?100:k==='trade'?98:k==='ghost'?70:90,s.chances[k]+(boost[k]||0))+'%'+(k==='raid'?' (호위 기본 '+s.guardRaid+'%)':'')+'</em></button>').join('');$('targetbox').hidden=mode!=='raid'}\nasync function order(cancel=false){if(!s||busy)return;busy=true;epoch++;try{let x=await api('/order',{day:s.day,r:s.r,mode:cancel?'cancel':mode,stake:Number($('stake').value),target:$('target').value,upgrade:$('upgrade').checked});if(x.error)throw Error(x.error);render(x,true);$('msg').textContent=cancel?'예약 취소 완료':'비밀 항해 예약 완료'}catch(e){$('msg').textContent=e.message}finally{busy=false}}\nfunction ranks(a){return a.map((q,i)=>'<div class=\"rank '+(q.name===s.name?'me':'')+'\"><b>'+String(i+1).padStart(2,'0')+'</b><div><b>'+esc(q.name)+'</b><br><small>선박 Lv. '+q.level+'</small></div><strong>'+fmt(q.balance)+'</strong></div>').join('')}\nfunction render(x,force=false){if(x.version!=='pirates-v1')return;let changed=!s||s.day!==x.day||s.r!==x.r;let histchanged=!s||JSON.stringify(s.history)!==JSON.stringify(x.history);let target=$('target').value;s=x;$('login').hidden=true;$('app').hidden=false;tab(view);$('round').textContent=x.active?'VOYAGE '+(x.r-17)+' / 30':'항구 휴항';$('phase').textContent=x.active?'출항까지':'오전 9시 개장';$('ready').textContent='준비 '+x.ready+' / 8';$('balance').textContent=fmt(x.balance);$('level').textContent='Lv. '+x.level;$('weather').textContent=x.weather;$('target').innerHTML='<option value=\"\">친구 선택</option>'+x.rank.filter(q=>q.name!==x.name).map(q=>'<option value=\"'+esc(q.name)+'\">'+esc(q.name)+'</option>').join('');$('target').value=target;if(changed||force){mode=x.own?x.own[0]:'trade';$('stake').value=x.own?x.own[1]:'';$('target').value=x.own?x.own[2]:'';$('upgrade').checked=x.own?x.own[3]:false}limits();$('confirm').disabled=!x.active;$('saved').textContent=x.own?'예약: '+modes[x.own[0]][1]+' · '+fmt(x.own[1])+(x.own[2]?' · 표적 '+x.own[2]:'')+(x.own[3]?' · 강화 포함':''):'아직 예약하지 않았습니다.';$('ranking').innerHTML=ranks(x.rank);$('finalrank').innerHTML=ranks(x.final);if(histchanged||force)$('records').innerHTML=x.history.map(h=>'<details class=\"history\"><summary>R'+(h.r-17)+' · '+esc(h.weather)+' · '+h.results.length+'명 출항</summary>'+h.results.map(q=>'<div class=\"rank\"><span>'+modes[q.mode][0]+'</span><div><b>'+esc(q.name)+'</b> '+esc(q.event)+'<br><small>'+esc(modes[q.mode][1])+' '+fmt(q.stake)+(q.target?' → '+esc(q.target):'')+(q.lost?' · 약탈 손실 '+fmt(q.lost):'')+'</small></div><strong class=\"'+(q.delta>=0?'plus':'minus')+'\">'+(q.delta>=0?'+':'')+fmt(q.delta)+'</strong></div>').join('')+'</details>').join('')||'<div class=\"empty\">아직 끝난 항해가 없습니다.</div>';tick()}\nasync function refresh(){if(!token||busy)return;let e=epoch;try{let x=await api('/state');if(e!==epoch||busy)return;if(x.error){$('err').textContent=x.error;return}render(x)}catch(e){$('msg').textContent='서버 연결 확인 중'}}function tick(){if(!s)return;let t=Math.max(0,s.end-Math.floor(Date.now()/1000));$('timer').textContent=[Math.floor(t/3600),Math.floor(t/60)%60,t%60].map(n=>String(n).padStart(2,'0')).join(':')}setInterval(tick,250);setInterval(refresh,7000);refresh();</script></html>"
