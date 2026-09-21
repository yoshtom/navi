import json,re,hashlib
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

JST=timezone(timedelta(hours=9)); NOW=datetime.now(JST); TODAY=NOW.date()
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 BosoRoadNavi/1.0','Accept-Language':'ja,en-US;q=0.8'}

# 収集源は「現在規制」が確認できる公式ページに限定。
NEXCO_INDEX='https://www.e-nexco.co.jp/news/important_info/'
KTR_CHIBA='https://www.ktr.mlit.go.jp/kisha/chiba_index.html'
PREF_ACTIVE=[
 ('千葉県','https://www.pref.chiba.lg.jp/doukan/press/2026/tsuukoukisei-0706.html'),
 ('千葉県','https://www.pref.chiba.lg.jp/doukan/press/2026/kisei260807.html'),
]
PREF_NEWS='https://www.pref.chiba.lg.jp/doukan/shinchaku.html'
MUNICIPAL=[
 ('君津市','https://www.city.kimitsu.lg.jp/soshiki/29/68941.html'),
 ('大多喜町','https://www.town.otaki.chiba.jp/kinkyu/2890.html'),
 ('木更津市','https://www.city.kisarazu.lg.jp/kurashi/doro_kotsu/kotsu/4/14124.html'),
 ('袖ケ浦市','https://www.city.sodegaura.lg.jp/soshiki/doboku-kanri/index-2.html'),
]
CLOSE_WORDS=('通行止め','通行止','全面通行止','道路閉鎖')
RELEASE_WORDS=('解除しました','通行止め解除','通行止解除','規制解除','解除のお知らせ','解除となりました','解除します')
FUTURE_WORDS=('通行止めとなる可能性','通行止めの可能性','予定しています','通行止め予定','規制予定')
CHIBA_CODES=('E14','E82','E51','E65','C4','CA')
CHIBA_HINTS=('千葉','木更津','富津','袖ヶ浦','袖ケ浦','姉崎','市原','茂原','東金','山田','高田','蘇我','富浦','成田','大栄','松尾横芝')

# 公式に区間が明示され、道路形状を追える既知の区間。
KNOWN_SEGMENTS=[
 {'contains':['国道127号','元名'], 'a':'鋸南町元名 国道127号','b':'富津市金谷 国道127号','motorway':False},
 {'contains':['国道127号','小浦'], 'a':'南房総市富浦町南無谷 国道127号','b':'南房総市富浦町小浦 国道127号','motorway':False},
]

def get(url,timeout=8):
 r=requests.get(url,headers=HEADERS,timeout=timeout);r.raise_for_status()
 if not r.encoding or r.encoding.lower()=='iso-8859-1': r.encoding=r.apparent_encoding
 return r

def norm(s): return re.sub(r'\s+',' ',s or '').strip()
def txt(node): return norm(node.get_text(' ',strip=True)) if node else ''
def hid(*p): return hashlib.sha1('|'.join(map(str,p)).encode()).hexdigest()[:14]

def infer_segment(title,road,location,desc):
 blob=' '.join([title,road,location,desc])
 for s in KNOWN_SEGMENTS:
  if all(k in blob for k in s['contains']):
   return {'queries':[s['a'],s['b']], 'motorway':s['motorway']}
 # 「A～B」「A〜B」で区間が明示されている場合
 m=re.search(r'([^、。|]{2,45}?)[～〜⇔]([^、。|]{2,45}?)(?:\s|$|、|。|\|)',blob)
 if m:
  a,b=norm(m.group(1)),norm(m.group(2))
  # あまり長い文章を端点にしない
  if len(a)<50 and len(b)<50:
   base=(road+' 千葉県').strip()
   return {'queries':[a+' '+base,b+' '+base], 'motorway':('高速' in blob or '自動車道' in blob or 'IC' in a+b or 'JCT' in a+b)}
 return None

def add(items,source,source_name,kind,title,road='',location='',desc='',query='',segment=None):
 blob=' '.join([title,desc,location])
 if any(w in blob for w in RELEASE_WORDS) and '実施中' not in blob: return
 if any(w in blob for w in FUTURE_WORDS): return
 segment=segment or infer_segment(title,road,location,desc)
 item={'id':hid(kind,title,location,source),'kind':kind,'status':'closed','title':norm(title),'road':norm(road),'location':norm(location),'description':norm(desc)[:650],'query':norm(query),'source':source,'source_name':source_name,'segment':segment,'precision':'segment' if segment else 'point'}
 items.append(item)

def parse_nexco_page(html,url,items):
 soup=BeautifulSoup(html,'html.parser');full=txt(soup)
 if '通行止め実施中' not in full:return 0
 h=None
 for hh in soup.find_all(['h1','h2','h3','h4']):
  if '通行止め実施中' in txt(hh): h=hh;break
 if not h:return 0
 tables=[]
 for sib in h.next_siblings:
  if getattr(sib,'name',None) in ['h1','h2','h3','h4']:break
  if getattr(sib,'name',None)=='table':tables.append(sib)
  elif hasattr(sib,'find_all'):tables.extend(sib.find_all('table'))
 added=0; current_road=''
 for table in tables:
  for tr in table.find_all('tr'):
   cells=[txt(x) for x in tr.find_all(['th','td'])]
   if len(cells)<2:continue
   joined=' | '.join(cells);first=cells[0]
   if any(code in first for code in CHIBA_CODES) or '自動車道' in first or '道路' in first:current_road=first
   road=current_road
   if not road:continue
   if not (any(code in road for code in CHIBA_CODES) or any(hh in joined for hh in CHIBA_HINTS)):continue
   sec=next((c for c in cells if re.search(r'(?:IC|JCT).*[～〜⇔].*(?:IC|JCT)',c)), '')
   if not sec:continue
   a,b=[norm(x) for x in re.split(r'[～〜⇔]',sec,maxsplit=1)]
   seg={'queries':[a+' '+road,b+' '+road],'motorway':True}
   add(items,url,'NEXCO東日本','highway',f'{road} {sec}',road,sec,joined,'',seg);added+=1
 return added

def collect_nexco(items):
 try: html=get(NEXCO_INDEX).text
 except Exception as e: print('nexco index',e);return
 soup=BeautifulSoup(html,'html.parser');links=[]
 for a in soup.find_all('a',href=True):
  label=txt(a); href=urljoin(NEXCO_INDEX,a['href'])
  if '通行止め' not in label:continue
  if any(w in label for w in ('解除','可能性','予定')):continue
  if '/news/important_info/' not in href:continue
  links.append(href)
 for u in list(dict.fromkeys(links))[:8]:
  try: parse_nexco_page(get(u).text,u,items)
  except Exception as e: print('nexco page',u,e)

def pref_released_keys():
 keys=set()
 try: html=get(PREF_NEWS).text
 except Exception as e: print('pref news',e);return keys
 soup=BeautifulSoup(html,'html.parser')
 links=[]
 for a in soup.find_all('a',href=True):
  label=txt(a)
  if '解除' not in label: continue
  if not any(x in label for x in ('通行規制','通行止め')):continue
  links.append(urljoin(PREF_NEWS,a['href']))
 for u in list(dict.fromkeys(links))[:15]:
  try:
   s=BeautifulSoup(get(u).text,'html.parser'); t=txt(s)
   m=re.search(r'((?:国道\d+号|県道[^｜\s（(]{2,20}(?:線)?)|県道[^｜\s]{2,20})[^｜\n]{0,40})[｜|]\s*([^。\n]{2,50})',t)
   if not m:
    m=re.search(r'((?:国道\d+号|県道[^（(\s]{2,20}))[^。]{0,30}?((?:[一-龥ぁ-んァ-ヶ]+市|[一-龥ぁ-んァ-ヶ]+町)[^。]{0,25}?地先)',t)
   if m: keys.add(re.sub(r'\s+','',m.group(1)+m.group(2)))
  except Exception as e: print('pref release',u,e)
 return keys

def parse_pref_active(html,url,items,released):
 soup=BeautifulSoup(html,'html.parser'); t=txt(soup)
 if not any(x in t for x in ('現在も全面通行止め','全面通行止めを行っています','通行規制を実施します','通行規制を継続')): return
 # 番号付き一覧
 found=[]
 for m in re.finditer(r'\(\d+\)\s*((?:国道\d+号|県道[^｜]{2,25}))\s*[｜|]\s*([^\s]{2,35}地先)',t):
  found.append((norm(m.group(1)),norm(m.group(2))))
 # 単独ページ
 if not found:
  m=re.search(r'(?:路線名|交通規制箇所).*?((?:国道\d+号|県道[^｜]{2,25}))\s*[｜|]?\s*((?:[一-龥ぁ-んァ-ヶ]+市|[一-龥ぁ-んァ-ヶ]+町)[^。\s]{0,35}地先)',t)
  if m: found.append((norm(m.group(1)),norm(m.group(2))))
 for road,loc in found:
  key=re.sub(r'\s+','',road+loc)
  if any(key in r or r in key for r in released): continue
  add(items,url,'千葉県','general',f'{road} {loc} 通行止め',road,loc,t,f'{road} {loc} 千葉県')

def collect_pref(items):
 released=pref_released_keys()
 for name,u in PREF_ACTIVE:
  try: parse_pref_active(get(u).text,u,items,released)
  except Exception as e: print('pref active',u,e)

def parse_ktr(html,url,items):
 soup=BeautifulSoup(html,'html.parser')
 for a in soup.find_all('a',href=True):
  label=txt(a)
  if '通行止めのお知らせ' not in label:continue
  if any(w in label for w in ('解除','可能性','予定')):continue
  # 直近の災害通行止めだけ。タイトルから路線・場所を抽出。
  roadm=re.search(r'(国道\s*\d+号)',label); road=roadm.group(1).replace(' ','') if roadm else ''
  loc=label.split('～')[-1].strip('～[]【】 ') if '～' in label else label
  if not road:continue
  add(items,url,'国土交通省 千葉国道事務所','general',label,road,loc,label,f'{road} {loc} 千葉県')

def collect_ktr(items):
 try: parse_ktr(get(KTR_CHIBA).text,KTR_CHIBA,items)
 except Exception as e: print('ktr',e)

def currentish(t):
 return ('現在' in t or '行っています' in t or '実施中' in t or '通行止めを実施' in t) and not ('すべて解除' in t)

def parse_municipal(html,url,name,items):
 soup=BeautifulSoup(html,'html.parser'); t=txt(soup)
 if not currentish(t): return
 # h2/h3や表の行を中心に、現在形の通行止め記述のみ。
 candidates=[]
 for el in soup.find_all(['tr','li','p','h2','h3','h4']):
  s=txt(el)
  if len(s)<4 or len(s)>500:continue
  if not any(w in s for w in CLOSE_WORDS):continue
  if any(w in s for w in RELEASE_WORDS+FUTURE_WORDS):continue
  candidates.append(s)
 for s in candidates[:40]:
  roadm=re.search(r'((?:国道\s*\d+号|県道\s*[^、。]{2,22}線|市道\s*[^、。]{1,25}(?:線|号)?|林道\s*[^、。]{1,25}線))',s)
  road=norm(roadm.group(1)) if roadm else ''
  locm=re.search(r'((?:[一-龥ぁ-んァ-ヶ]+市|[一-龥ぁ-んァ-ヶ]+町)[^、。]{0,45}?(?:地先|付近|隧道|トンネル|橋|交差点))',s)
  loc=norm(locm.group(1)) if locm else ''
  if not road and not loc:continue
  add(items,url,name,'general',f'{road or loc} 通行止め',road,loc,s,' '.join(x for x in [road,loc,name,'千葉県'] if x))

def collect_municipal(items):
 def f(x):
  n,u=x
  try:return n,u,get(u).text,None
  except Exception as e:return n,u,None,e
 with ThreadPoolExecutor(max_workers=4) as ex:
  for fu in as_completed([ex.submit(f,x) for x in MUNICIPAL]):
   n,u,h,e=fu.result()
   if e: print('municipal',n,e);continue
   try: parse_municipal(h,u,n,items)
   except Exception as e: print('parse municipal',n,e)

def dedupe(xs):
 out=[];seen=set()
 for x in xs:
  key=(x['kind'],re.sub(r'\s+','',x.get('road','')),re.sub(r'\s+','',x.get('location','') or x['title']))
  if key in seen:continue
  seen.add(key);out.append(x)
 return out

def main():
 items=[]
 collect_nexco(items); collect_pref(items); collect_ktr(items); collect_municipal(items)
 items=dedupe(items)
 general=[x for x in items if x['kind']=='general']; high=[x for x in items if x['kind']=='highway']
 data={'generated_at':datetime.now(JST).isoformat(timespec='seconds'),'area':'房総半島・千葉県・東京湾アクアライン周辺','count':len(items),'general_count':len(general),'highway_count':len(high),'closures':items,'note':'現在実施中の公式規制に限定。ルート計算では各規制地点をMapboxのpoint exclusionへ渡し、返却ルートも規制への接触を再検査する。区間が明示された規制は道路ネットワーク沿いに黒線表示。'}
 out=Path(__file__).resolve().parent/'docs'/'closures.json';out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');print('saved',len(general),len(high),out)
if __name__=='__main__':main()
