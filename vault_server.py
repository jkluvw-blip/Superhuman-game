import os, json, secrets, hashlib, hmac, time, random
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock
from urllib.parse import urlsplit
import psycopg
import pirate_game
K=timezone(timedelta(hours=9)); lock=Lock()
def connect():
    return psycopg.connect(os.environ["DATABASE_URL"])
def setup():
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS players(name text PRIMARY KEY, salt text, pin text, token text UNIQUE)")
        c.execute("CREATE TABLE IF NOT EXISTS bids(day text, r int, name text, amount bigint, PRIMARY KEY(day,r,name))")
        c.execute("CREATE TABLE IF NOT EXISTS results(day text,r int,detail text,PRIMARY KEY(day,r))")
        c.execute("CREATE TABLE IF NOT EXISTS auction_config(id int PRIMARY KEY, seed text NOT NULL)")
        c.execute("INSERT INTO auction_config VALUES(1,%s) ON CONFLICT DO NOTHING",(secrets.token_hex(32),))
        c.execute("CREATE TABLE IF NOT EXISTS auction_bids(day text,r int,lot int,name text,amount bigint,stamp timestamptz DEFAULT clock_timestamp(),PRIMARY KEY(day,r,lot,name))")
        c.execute("CREATE TABLE IF NOT EXISTS auction_actions(day text,r int,name text,kind text,target text,PRIMARY KEY(day,name,kind,target))")
def clock():
    d=datetime.now(K); return d,d.strftime("%Y-%m-%d"),d.hour*2+d.minute//30
CAT=[
 ("황금 회중시계","골동품","왕실", "watch",180000,540000,80,"희귀"),
 ("왕의 인장","골동품","왕실","seal",260000,920000,65,"영웅"),
 ("사라진 왕관","보석","왕실","crown",450000,1850000,55,"전설"),
 ("붉은 루비 반지","보석","불꽃","gem",130000,950000,70,"희귀"),
 ("태양의 목걸이","보석","불꽃","necklace",250000,1200000,65,"영웅"),
 ("불사조 조각상","골동품","불꽃","statue",320000,1500000,60,"전설"),
 ("푸른 사파이어","보석","심해","gem",90000,470000,90,"일반"),
 ("침몰선 나침반","골동품","심해","watch",150000,650000,80,"희귀"),
 ("해왕의 삼지창","골동품","심해","statue",400000,1600000,58,"전설"),
 ("달빛 풍경화","미술품","달빛","painting",100000,390000,90,"일반"),
 ("은빛 가면","미술품","달빛","mask",160000,750000,75,"희귀"),
 ("잃어버린 명화","미술품","달빛","painting",350000,1900000,55,"전설"),
 ("빈티지 만년필","골동품","","seal",45000,180000,95,"일반"),
 ("봉인된 금고","미스터리","","safe",0,2800000,45,"신화"),
 ("고대 금화","골동품","","seal",65000,320000,88,"일반"),
 ("흑진주","보석","","gem",120000,600000,80,"희귀"),
 ("수수께끼의 상자","미스터리","","safe",0,1400000,60,"영웅"),
 ("황금 가면","미술품","","mask",250000,1300000,65,"영웅"),
 ("옥 조각상","골동품","","statue",90000,400000,85,"일반"),
 ("다이아몬드 브로치","보석","","necklace",320000,1700000,62,"전설"),
 ("잊힌 초상화","미술품","","painting",80000,450000,85,"일반"),
 ("청동 봉인함","미스터리","","safe",40000,700000,70,"희귀"),
 ("황제의 다이아 왕관","보석","", "crown",700000,3200000,52,"신화"),
 ("백금 시계","골동품","","watch",180000,900000,75,"영웅")]
CATS=["보석","골동품","미술품","미스터리"]
FEE=5000
def rng(seed,*parts):
    return random.Random(int.from_bytes(hmac.digest(seed.encode(),("|".join(map(str,parts))).encode(),"sha256"),"big"))
def lots(seed,day,r):
    randomizer=rng(seed,day,r,"catalog")
    pool=list(range(len(CAT)))
    if r==47:
        ids=[randomizer.choice([13,22]),randomizer.choice([2,5,8,11,19])]
    else:
        ids=randomizer.sample(pool,2)
    out=[]
    for j,k in enumerate(ids):
        name,cat,group,icon,lo,hi,p,grade=CAT[k]
        z=rng(seed,day,r,j,"value")
        genuine=z.randrange(100)<p
        base=z.randint(lo,hi) if genuine else z.randint(0,max(1000,hi//14))
        reserve=max(1000,int((lo*p/100+hi*p/100)*.15)//1000*1000)
        condition=z.choice(["양호","보통","사용 흔적","보존 우수"])
        out.append(dict(id=j,key=f"{r}:{j}",name=name,cat=cat,group=group,icon=icon,lo=lo,hi=hi,p=p,grade=grade,base=base,genuine=genuine,reserve=reserve,condition=condition))
    return out
def market(seed,day,r):
    m={k:100 for k in CATS}; news="시장 안정 · 기본 시세"
    for i in range(18,min(r,47)+1):
        z=rng(seed,day,i,"market")
        if z.random()<.52:
            k=z.choice(CATS); delta=z.choice([-20,-15,-10,10,15,20,30])
            before=m[k]; m[k]=min(170,max(55,before+delta))
            if i==min(r,47):
                news=f"{k} {'수요 급증' if delta>0 else '매물 증가'} · 시세 지수 {before} → {m[k]}"
    return m,news
def mark(item,m):
    return item["base"]*m[item["cat"]]//100
def setbonus(items,m):
    by={}
    for q in items.values():
        if q["group"] and q["genuine"]:
            by.setdefault(q["group"],{})[q["name"]]=q
    return sum(sum(mark(q,m) for q in d.values())//4 for d in by.values() if len(d)>=3)
def public(q):
    return {k:v for k,v in q.items() if k not in ("base","genuine")}
def auction_state(c,day,r):
    seed=c.execute("SELECT seed FROM auction_config WHERE id=1").fetchone()[0]
    cash={n:1000000 for (n,) in c.execute("SELECT name FROM players")}
    inv={n:{} for n in cash}; history=[]; hints={n:set() for n in cash}
    bids={}; actions={}
    for i,j,n,a in c.execute("SELECT r,lot,name,amount FROM auction_bids WHERE day=%s ORDER BY stamp,name",(day,)):
        if n in cash: bids.setdefault((i,j),[]).append((n,a))
    for i,n,k,target in c.execute("SELECT r,name,kind,target FROM auction_actions WHERE day=%s ORDER BY kind,target,name",(day,)):
        if n in cash: actions.setdefault(i,[]).append((n,k,target))
    for i in range(18,min(r,48)+1):
        if i==48: break
        m,_=market(seed,day,i)
        for n,k,target in actions.get(i,[]):
            if k=="appraise":
                cash[n]-=FEE; hints[n].add(target)
            elif k=="sell" and target in inv[n]:
                cash[n]+=mark(inv[n].pop(target),m)*9//10
        if i>=r: break
        for q in lots(seed,day,i):
            bb=sorted(bids.get((i,q["id"]),[]),key=lambda x:-x[1])
            if not bb: continue
            winner,offer=bb[0]
            price=min(offer,max(q["reserve"],bb[1][1]+1 if len(bb)>1 else q["reserve"]))
            cash[winner]-=price
            item=dict(q,paid=price)
            inv[winner][q["key"]]=item
            history.append(dict(r=i,lot=q["name"],winner=winner,price=price,genuine=q["genuine"],value=mark(q,m),bids=bb))
    m,news=market(seed,day,r)
    ranks=[]
    for n,v in cash.items():
        assets=sum(mark(q,m) for q in inv[n].values()); bonus=setbonus(inv[n],m)
        ranks.append(dict(name=n,cash=v,assets=assets,bonus=bonus,total=v+assets+bonus))
    ranks.sort(key=lambda q:(-q["total"],q["name"]))
    return seed,cash,inv,history,hints,bids,m,news,ranks
def digest(pin,salt): return hashlib.scrypt(pin.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
class H(BaseHTTPRequestHandler):
    def send(self,data,status=200,html=False):
        b=data.encode() if html else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","text/html; charset=utf-8" if html else "application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self.path=urlsplit(self.path).path
        if self.path=="/pirates" or self.path.startswith("/pirates/"): return pirate_game.handle(self)
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
        if self.path.startswith("/pirates/"): return pirate_game.handle(self,p)
        if self.path not in ("/join","/bid","/appraise","/sell"): return self.send({"error":"Not found"},404)
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
                n=row[0]
                seed,cash,inv,hist,hints,bids,m,news,ranking=auction_state(c,day,r)
                active=r>=18
                own={j:next((a for who,a in bids.get((r,j),[]) if who==n),0) for j in range(2)}
                if self.path in ("/bid","/appraise","/sell"):
                    if not active or p.get("day")!=day or p.get("r")!=r: return self.send({"error":"라운드 마감 또는 휴장 중"},400)
                    j=p.get("lot")
                    if self.path in ("/bid","/appraise") and (type(j)!=int or j not in (0,1)): return self.send({"error":"물품 오류"},400)
                    if self.path=="/bid":
                        a=p.get("amount"); q=lots(seed,day,r)[j]
                        if type(a)!=int or a<0 or (a and a<q["reserve"]) or a+own[1-j]>cash[n]: return self.send({"error":"최소 입찰가와 사용 가능 코인을 확인하세요."},400)
                        if a: c.execute("INSERT INTO auction_bids(day,r,lot,name,amount) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(day,r,lot,name) DO UPDATE SET amount=excluded.amount,stamp=clock_timestamp()",(day,r,j,n,a))
                        else: c.execute("DELETE FROM auction_bids WHERE day=%s AND r=%s AND lot=%s AND name=%s",(day,r,j,n))
                    elif self.path=="/appraise":
                        target=f"{r}:{j}"
                        if target not in hints[n] and cash[n]-sum(own.values())<FEE: return self.send({"error":"감정 비용 5,000 C가 필요합니다."},400)
                        c.execute("INSERT INTO auction_actions VALUES(%s,%s,%s,'appraise',%s) ON CONFLICT DO NOTHING",(day,r,n,target))
                    else:
                        target=p.get("target")
                        if not isinstance(target,str) or target not in inv[n]: return self.send({"error":"판매할 물품이 없습니다."},400)
                        c.execute("INSERT INTO auction_actions VALUES(%s,%s,%s,'sell',%s) ON CONFLICT DO NOTHING",(day,r,n,target))
                    seed,cash,inv,hist,hints,bids,m,news,ranking=auction_state(c,day,r)
                    own={j:next((a for who,a in bids.get((r,j),[]) if who==n),0) for j in range(2)}
                items=[]
                if active:
                    for q in lots(seed,day,r):
                        p2=public(q)
                        p2["own"]=own[q["id"]]; p2["count"]=len(bids.get((r,q["id"]),[]))
                        if q["key"] in hints[n]:
                            z=rng(seed,day,r,q["id"],n,"hint")
                            likely=q["genuine"] if z.random()<.85 else not q["genuine"]
                            p2["hint"]="감정 소견: "+("진품 가능성이 높음" if likely else "위조 의심")+" (정확도 85%)"
                        items.append(p2)
                warehouse=[dict(q,value=mark(q,m),sell=mark(q,m)*9//10) for q in inv[n].values()]
                prev=(d-timedelta(days=1)).strftime("%Y-%m-%d"); final=auction_state(c,prev,48)[-1]
                end=d.replace(hour=9,minute=0,second=0,microsecond=0) if r<18 else d.replace(minute=d.minute//30*30,second=0,microsecond=0)+timedelta(minutes=30)
                c.commit()
                self.send(dict(version="auction-v2",name=n,day=day,r=r,end=int(end.timestamp()),active=active,balance=cash[n],available=cash[n]-sum(own.values()),rank=ranking,items=items,warehouse=warehouse,history=hist[::-1],final=final,market=m,news=news))
        except Exception as e:
            print(type(e).__name__,str(e),flush=True); self.send({"error":"서버 오류"},500)
PAGE="<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>비밀금고 : 암시장</title><style>\n:root{--gold:#e4bd78;--dim:#9196a3;--bg:#0b0e13;--panel:#131820;--line:#2b303a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 70% 0,#282116,#0b0e13 45%);color:#eeeef2;font:15px system-ui,sans-serif;min-height:100vh}main{max-width:1040px;margin:auto;padding:24px 16px 80px}header{display:flex;align-items:center;justify-content:space-between;gap:12px}h1{font-size:clamp(26px,5vw,42px);letter-spacing:-1px;margin:5px 0}h2{font-size:17px;margin:0 0 16px}small,.dim{color:var(--dim);line-height:1.55}.eyebrow{font-size:10px;font-weight:800;color:var(--gold);letter-spacing:3px}.pill{padding:6px 10px;border:1px solid #6e5839;border-radius:20px;color:var(--gold);font-size:11px;white-space:nowrap}.panel{background:linear-gradient(145deg,#191e27,#11161d);border:1px solid var(--line);border-radius:18px;padding:18px;margin:14px 0;box-shadow:0 8px 24px #0004}.hero{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:center}.timer{font-variant-numeric:tabular-nums;font-weight:800;font-size:clamp(24px,5vw,40px);color:var(--gold);letter-spacing:1px}.cash{font-size:clamp(22px,4vw,32px);font-weight:800}.market{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.market span{border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px}.up{color:#7dd9b1}.down{color:#ff9386}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.item{overflow:hidden;border:1px solid #383b44;border-radius:16px;background:#151a22;position:relative}.art{height:170px;display:grid;place-items:center;background:radial-gradient(ellipse at 50% 60%,#66543c,#262728 45%,#10141a 75%);position:relative;overflow:hidden}.art:before{content:\"\";position:absolute;inset:10px;border:1px solid #ffffff12;border-radius:8px}.art svg{width:124px;height:124px;filter:drop-shadow(0 12px 12px #0009)}.grade{position:absolute;top:12px;left:12px;font-size:11px;font-weight:700;color:#fff;background:#111a;border:1px solid currentColor;border-radius:5px;padding:4px 8px}.g희귀{color:#82ceff}.g영웅{color:#c6a0ff}.g전설{color:#ffc16c}.g신화{color:#ff819a}.details{padding:15px}.name{font-size:19px;font-weight:800;letter-spacing:-.5px;margin:0 0 5px}.info{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin:8px 0;color:var(--dim)}.info b{color:#eae7df}.value{color:var(--gold);font-size:18px;font-weight:700}.actions{display:flex;gap:6px;margin:8px 0}.actions>*{flex:1;min-width:0}input,button{font:inherit;border-radius:9px;padding:12px;border:1px solid #3b424d}input{background:#0a0e13;color:white;width:100%;margin:5px 0}button{background:var(--gold);color:#17120c;font-weight:800;cursor:pointer}button.alt{background:#222a34;color:#dddfe5}button:disabled{opacity:.4;cursor:default}.tabs{display:flex;gap:8px;position:sticky;top:0;z-index:3;padding:8px 0;background:#0d1016}.tabs button{flex:1;background:#1a202a;color:#adb1bb;font-size:13px;padding:10px 3px}.tabs button.on{background:#e4bd78;color:#17120c}.rline{display:grid;grid-template-columns:32px 1fr auto;gap:9px;padding:12px 0;border-bottom:1px solid var(--line);align-items:center}.rline strong{color:var(--gold);text-align:right}.me{color:var(--gold)}.msg{color:#f3bd84;min-height:20px;font-size:13px}.hint{border-left:2px solid var(--gold);padding:6px 9px;font-size:12px;color:var(--gold)}.history{border-bottom:1px solid var(--line);padding:13px 0}summary{cursor:pointer}.empty{text-align:center;padding:36px 12px;color:var(--dim)}[hidden]{display:none!important}@media(max-width:620px){main{padding:16px 10px 60px}.panel{padding:13px}.cards{grid-template-columns:1fr}.art{height:150px}.hero{gap:10px}.details{padding:12px}}\n</style><main><header><div><div class=\"eyebrow\">SECRET VAULT / PRIVATE AUCTION</div><h1>비밀금고 <span style=\"color:var(--gold)\">암시장</span></h1><small>가치를 읽는 자가 시장을 지배한다.</small></div><span class=\"pill\">8인 클럽</span></header><section class=\"panel\" id=\"login\"><h2>경매장 입장</h2><input id=\"nick\" placeholder=\"닉네임\" maxlength=\"12\"><input id=\"pin\" type=\"password\" placeholder=\"개인 비밀번호 (6자 이상)\"><input id=\"code\" type=\"password\" placeholder=\"방 코드\"><button onclick=\"join()\">입장하기</button><p id=\"err\" class=\"msg\"></p></section><div id=\"app\" hidden><section class=\"panel hero\"><div><div class=\"eyebrow\" id=\"round\"></div><div id=\"timer\" class=\"timer\"></div><small id=\"phase\">경매 마감까지</small></div><div><small>보유 코인</small><div class=\"cash\" id=\"balance\"></div><small id=\"available\"></small></div></section><section class=\"panel\"><div class=\"eyebrow\">MARKET WIRE</div><p id=\"news\" style=\"margin:8px 0;font-weight:700\"></p><div class=\"market\" id=\"market\"></div></section><div class=\"tabs\"><button id=\"tauction\" onclick=\"tab('auction')\">경매장</button><button id=\"twarehouse\" onclick=\"tab('warehouse')\">내 창고</button><button id=\"trank\" onclick=\"tab('rank')\">순위</button><button id=\"thistory\" onclick=\"tab('history')\">낙찰 기록</button></div><p id=\"msg\" class=\"msg\"></p><section id=\"auction\" class=\"panel\"><h2>이번 라운드 출품작</h2><div class=\"cards\" id=\"lots\"></div><p class=\"dim\">비공개 입찰 · 최고액 낙찰, 실제 지불액은 2등가 + 1 C (최소가 이상). 단독 입찰은 최소가, 동점은 먼저 제출한 입찰 우선.</p></section><section id=\"warehouse\" class=\"panel\" hidden><h2>내 수집품</h2><div id=\"assetstat\"></div><div class=\"cards\" id=\"stock\"></div></section><section id=\"rank\" class=\"panel\" hidden><h2>실시간 총자산 순위</h2><div id=\"ranking\"></div><h2 style=\"margin-top:24px\">어제 최종 순위</h2><div id=\"finalrank\"></div></section><section id=\"history\" class=\"panel\" hidden><h2>공개 낙찰 기록</h2><div id=\"records\"></div></section><section class=\"panel\"><h2>거래 규칙</h2><p class=\"dim\">매일 09:00~24:00 · 30분마다 2개 물품. 시작 자본 100만 C. 감정 비용 5,000 C, 진품 판단 정확도 85%. 소장품 평가액은 시세에 따라 바뀌며 즉시 판매는 평가액의 90%. 같은 세트의 서로 다른 진품 3종을 모으면 해당 3종 평가액의 25% 보너스. 자정에 총자산으로 승부, 다음 날 자산 초기화. 실제 현금 충전·환전 없음.</p><button class=\"alt\" onclick=\"localStorage.removeItem('vaultToken');location.reload()\">로그아웃</button></section></div></main><script>\nlet token=localStorage.getItem('vaultToken')||'',s,view='auction',busy=false,epoch=0;const $=x=>document.getElementById(x),fmt=x=>Number(x).toLocaleString('ko-KR')+' C',esc=x=>String(x).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\nfunction art(k){const paths={watch:'<circle cx=\"60\" cy=\"65\" r=\"40\" fill=\"url(#g)\" stroke=\"#f5d59a\" stroke-width=\"3\"/><circle cx=\"60\" cy=\"65\" r=\"31\" fill=\"#f3dfb8\" stroke=\"#77512e\" stroke-width=\"3\"/><path d=\"M60 43v23l17 10M60 35v7M60 88v7M30 65h7M83 65h7\" stroke=\"#533c2b\" stroke-width=\"3\" stroke-linecap=\"round\"/><rect x=\"53\" y=\"11\" width=\"14\" height=\"14\" rx=\"5\" fill=\"url(#g)\"/>',gem:'<path d=\"M22 36 42 18h36l20 18-38 65z\" fill=\"#97304c\" stroke=\"#ffafbc\" stroke-width=\"2\"/><path d=\"M22 36h76L60 101 42 36 60 18 78 36 60 101\" fill=\"#ed7a92\" fill-opacity=\".65\" stroke=\"#ffc4cd\" stroke-width=\"1.5\"/>',crown:'<path d=\"m18 35 22 23 20-34 20 34 22-23-12 51H30z\" fill=\"url(#g)\" stroke=\"#f7dc9f\" stroke-width=\"3\"/><rect x=\"28\" y=\"86\" width=\"64\" height=\"12\" rx=\"4\" fill=\"#cc9c4f\"/><circle cx=\"60\" cy=\"67\" r=\"10\" fill=\"#b63251\" stroke=\"#ffe1a1\" stroke-width=\"3\"/>',safe:'<rect x=\"13\" y=\"17\" width=\"94\" height=\"86\" rx=\"10\" fill=\"#525961\" stroke=\"#a4a9b0\" stroke-width=\"3\"/><rect x=\"22\" y=\"26\" width=\"76\" height=\"68\" rx=\"6\" fill=\"#262d35\" stroke=\"#87919b\" stroke-width=\"2\"/><circle cx=\"60\" cy=\"60\" r=\"22\" fill=\"url(#g)\" stroke=\"#262d35\" stroke-width=\"3\"/><path d=\"M60 40v40M40 60h40\" stroke=\"#2a2e33\" stroke-width=\"5\"/><circle cx=\"60\" cy=\"60\" r=\"6\" fill=\"#f4d796\"/>',painting:'<rect x=\"18\" y=\"12\" width=\"84\" height=\"96\" rx=\"3\" fill=\"url(#g)\"/><rect x=\"27\" y=\"21\" width=\"66\" height=\"78\" fill=\"#496675\"/><circle cx=\"72\" cy=\"43\" r=\"13\" fill=\"#e6c990\"/><path d=\"M27 88 48 53 66 76 81 58 93 75v24H27\" fill=\"#253e43\"/>',necklace:'<path d=\"M22 20c-4 59 80 59 76 0\" fill=\"none\" stroke=\"#eac17a\" stroke-width=\"6\" stroke-dasharray=\"5 3\"/><path d=\"m60 62-15 17 15 25 15-25z\" fill=\"#5da2aa\" stroke=\"#f0d89d\" stroke-width=\"4\"/>',seal:'<circle cx=\"60\" cy=\"60\" r=\"39\" fill=\"url(#g)\" stroke=\"#f2d58d\" stroke-width=\"3\"/><circle cx=\"60\" cy=\"60\" r=\"31\" fill=\"none\" stroke=\"#7c512b\" stroke-width=\"2\" stroke-dasharray=\"3 4\"/><path d=\"m60 34 8 17 19 3-14 13 3 19-16-9-16 9 3-19-14-13 19-3z\" fill=\"#f7d799\" stroke=\"#8d602e\" stroke-width=\"2\"/>',mask:'<path d=\"M20 26Q60 5 100 26L92 68 60 102 28 68Z\" fill=\"url(#g)\" stroke=\"#f3daaa\" stroke-width=\"3\"/><path d=\"M30 48q16-14 24 3-12 12-24-3M90 48q-16-14-24 3 12 12 24-3M48 78h24\" fill=\"#24262c\" stroke=\"#7d5228\" stroke-width=\"3\"/>',statue:'<path d=\"M50 18 70 18 76 34 66 49 91 78 78 88 60 67 42 88 29 78 54 49 44 34Z\" fill=\"url(#g)\" stroke=\"#f5d99a\" stroke-width=\"2\"/><rect x=\"35\" y=\"90\" width=\"50\" height=\"12\" fill=\"#ac8144\" stroke=\"#dcb575\"/>'};return '<svg viewBox=\"0 0 120 120\"><defs><linearGradient id=\"g\" x2=\"1\" y2=\"1\"><stop stop-color=\"#fff0b2\"/><stop offset=\".46\" stop-color=\"#ba8845\"/><stop offset=\".7\" stop-color=\"#f2d295\"/><stop offset=\"1\" stop-color=\"#76512a\"/></linearGradient></defs>'+ (paths[k]||paths.safe)+'</svg>'}\nasync function api(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':token},body:data?JSON.stringify(data):undefined});return r.json()}\nasync function join(){try{let x=await api('/join',{name:$('nick').value,pin:$('pin').value,code:$('code').value});if(x.error)throw Error(x.error);token=x.token;localStorage.setItem('vaultToken',token);refresh()}catch(e){$('err').textContent=e.message}}\nfunction tab(t){view=t;for(let k of ['auction','warehouse','rank','history']){$(k).hidden=k!==t;$('t'+k).classList.toggle('on',k===t)}}\nasync function act(path,data){if(busy||!s)return;busy=true;epoch++;try{let x=await api(path,{...data,day:s.day,r:s.r});if(x.error)throw Error(x.error);render(x,true);$('msg').textContent=path==='/bid'?(data.amount?'비밀 입찰이 저장되었습니다.':'입찰 취소 완료'):path==='/sell'?'판매 완료':'감정 완료'}catch(e){$('msg').textContent=e.message}finally{busy=false}}\nfunction bid(j,zero=false){act('/bid',{lot:j,amount:zero?0:Number($('a'+j).value)})}\nfunction preset(j){let q=s.items.find(x=>x.id===j);$('a'+j).value=Math.max(0,s.available+q.own)}\nfunction lot(q){return '<article class=\"item\"><div class=\"art\">'+art(q.icon)+'<span class=\"grade g'+q.grade+'\">'+q.grade+'</span></div><div class=\"details\"><div class=\"name\">'+esc(q.name)+'</div><small>'+esc(q.cat)+' · '+esc(q.condition)+(q.group?' · '+esc(q.group)+' 세트':'')+'</small><p class=\"value\">'+fmt(q.lo)+' ~ '+fmt(q.hi)+'</p><div class=\"info\"><span>진품 확률 <b>'+q.p+'%</b></span><span>참여 <b>'+q.count+'명</b></span></div><div class=\"info\"><span>최소가 <b>'+fmt(q.reserve)+'</b></span><span>내 입찰 <b>'+fmt(q.own)+'</b></span></div>'+(q.hint?'<p class=\"hint\">'+esc(q.hint)+'</p>':'<button class=\"alt\" style=\"width:100%\" onclick=\"act(\\'/appraise\\',{lot:'+q.id+'})\">감정 의뢰 · 5,000 C</button>')+'<input id=\"a'+q.id+'\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"입찰 금액\" value=\"'+(q.own||'')+'\"><div class=\"actions\"><button onclick=\"bid('+q.id+')\">비밀 입찰</button><button class=\"alt\" onclick=\"preset('+q.id+')\">최대</button><button class=\"alt\" onclick=\"bid('+q.id+',true)\">취소</button></div></div></article>'}\nfunction stock(q){return '<article class=\"item\"><div class=\"art\">'+art(q.icon)+'<span class=\"grade g'+q.grade+'\">'+q.grade+'</span></div><div class=\"details\"><div class=\"name\">'+esc(q.name)+'</div><small>'+ (q.genuine?'진품':'가품')+(q.group?' · '+esc(q.group)+' 세트':'')+'</small><p class=\"value\">'+fmt(q.value)+'</p><div class=\"info\"><span>구매가 '+fmt(q.paid)+'</span><span>손익 '+fmt(q.value-q.paid)+'</span></div><button class=\"alt\" style=\"width:100%\" '+(!s.active?'disabled':'')+' onclick=\"act(\\'/sell\\',{target:\\''+q.key+'\\'})\">즉시 판매 '+fmt(q.sell)+'</button></div></article>'}\nfunction ranks(a){return a.map((q,i)=>'<div class=\"rline '+(q.name===s.name?'me':'')+'\"><b>'+String(i+1).padStart(2,'0')+'</b><div><b>'+esc(q.name)+'</b><br><small>코인 '+fmt(q.cash)+' · 수집품 '+fmt(q.assets)+' · 세트 '+fmt(q.bonus)+'</small></div><strong>'+fmt(q.total)+'</strong></div>').join('')}\nfunction render(x,force=false){if(x.version!=='auction-v2')return;let changed=!s||s.day!==x.day||s.r!==x.r||JSON.stringify(s.items)!==JSON.stringify(x.items);let draft=force||!s||s.r!==x.r||s.day!==x.day?{}:Object.fromEntries(s.items.map(q=>[q.id,$('a'+q.id)?.value]));let warehouseChanged=!s||JSON.stringify(s.warehouse)!==JSON.stringify(x.warehouse);let hChanged=!s||JSON.stringify(s.history)!==JSON.stringify(x.history);s=x;$('login').hidden=true;$('app').hidden=false;tab(view);$('round').textContent=x.active?'ROUND '+(x.r-17)+' / 30':'MARKET CLOSED';$('phase').textContent=x.active?'경매 마감까지':'오전 9시 개장';$('balance').textContent=fmt(x.balance);$('available').textContent='입찰 가능 '+fmt(x.available);$('news').textContent=x.news;$('market').innerHTML=Object.entries(x.market).map(([k,v])=>'<span>'+esc(k)+' <b class=\"'+(v>=100?'up':'down')+'\">'+v+'%</b></span>').join('');if(changed||force){$('lots').innerHTML=x.items.map(lot).join('')||'<div class=\"empty\">경매장이 닫혀 있습니다.</div>';for(let [k,v] of Object.entries(draft))if(v!==undefined&&$('a'+k))$('a'+k).value=v}if(warehouseChanged||force)$('stock').innerHTML=x.warehouse.map(stock).join('')||'<div class=\"empty\">아직 낙찰받은 물품이 없습니다.</div>';let me=x.rank.find(q=>q.name===x.name);$('assetstat').innerHTML='<p class=\"value\">평가액 '+fmt(me.assets)+' · 세트 보너스 '+fmt(me.bonus)+'</p>';$('ranking').innerHTML=ranks(x.rank);$('finalrank').innerHTML=ranks(x.final);if(hChanged||force)$('records').innerHTML=x.history.map(h=>'<details class=\"history\"><summary>R'+(h.r-17)+' · '+esc(h.lot)+' · '+esc(h.winner)+' 낙찰 '+fmt(h.price)+'</summary><p>'+(h.genuine?'진품':'가품')+' · 공개 시 평가액 '+fmt(h.value)+'</p>'+h.bids.map(([n,a])=>'<div class=\"info\"><span>'+esc(n)+'</span><b>'+fmt(a)+'</b></div>').join('')+'</details>').join('')||'<div class=\"empty\">아직 낙찰 기록이 없습니다.</div>';tick()}\nasync function refresh(){if(!token||busy)return;let stamp=epoch;try{let x=await api('/state');if(stamp!==epoch||busy)return;if(x.error){$('err').textContent=x.error;return}render(x)}catch(e){$('msg').textContent='서버 연결 확인 중'}}function tick(){if(!s)return;let t=Math.max(0,s.end-Math.floor(Date.now()/1000));$('timer').textContent=[Math.floor(t/3600),Math.floor(t/60)%60,t%60].map(n=>String(n).padStart(2,'0')).join(':')}setInterval(tick,250);setInterval(refresh,7000);refresh();</script></html>"
if __name__=="__main__":
    setup()
    pirate_game.setup()
    pirate_game.selftest()
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
                status,body=probe("/state",token=token)
                assert status==200 and json.loads(body)["version"]=="auction-v2"
                assert len(json.loads(body)["items"]) in (0,2)
                assert json.loads(body)["balance"]==1000000
                print("AUCTION V2 SELFTEST PASS: catalog, balance, login and state",flush=True)
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
    try:
        assert probe("/pirates?utm_source=chatgpt.com")[0]==200
        code=os.environ["ROOM_CODE"]
        assert probe("/pirates/join",{"name":"","pin":"","code":code})[0]==400
        assert probe("/pirates/join",{"name":"","pin":"","code":code+"!"})[0]==403
        with pirate_game.connect() as c:
            count=c.execute("SELECT count(*) FROM pirate_players").fetchone()[0]
        if count<8:
            name="T"+secrets.token_hex(5); pin=secrets.token_hex(12)
            try:
                status,body=probe("/pirates/join",{"name":name,"pin":pin,"code":code})
                assert status==200
                tok=json.loads(body)["token"]
                status,body=probe("/pirates/state",token=tok)
                st=json.loads(body)
                assert status==200 and st["version"]=="pirates-v1" and st["balance"]==1000000
                if st["active"]:
                    data={"day":st["day"],"r":st["r"],"mode":"trade","stake":1000,"upgrade":False}
                    status,body=probe("/pirates/order",data,token=tok)
                    assert status==200 and json.loads(body)["own"][1]==1000
                    data["mode"]="cancel"
                    status,body=probe("/pirates/order",data,token=tok)
                    assert status==200 and json.loads(body)["own"] is None
                print("PIRATE HTTP SELFTEST PASS: page, room code, signup, state and order API",flush=True)
            finally:
                with pirate_game.connect() as c:
                    c.execute("DELETE FROM pirate_orders WHERE name=%s",(name,))
                    c.execute("DELETE FROM pirate_players WHERE name=%s",(name,))
        else:
            print("PIRATE HTTP SELFTEST PASS: page and room code; signup skipped because room is full",flush=True)
    except Exception as e:
        print("PIRATE HTTP SELFTEST FAILED:",type(e).__name__,str(e),flush=True)
    from threading import Event
    Event().wait()
