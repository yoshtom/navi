import json,re,hashlib
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

JST=timezone(timedelta(hours=9)); NOW=datetime.now(JST); TODAY=NOW.date()
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 BosoRoadNavi/0.8','Accept-Language':'ja,en-US;q=0.8'}

# 「現在の規制」を載せるページを中心に限定。広いカテゴリ巡回はしない。
SEEDS=[
 ('NEXCO東日本','https://www.e-nexco.co.jp/news/important_info/'),
 ('君津市','https://www.city.kimitsu.lg.jp/soshiki/29/68941.html'),
 ('大多喜町','https://www.town.otaki.chiba.jp/kinkyu/2890.html'),
 ('木更津市','https://www.city.kisarazu.lg.jp/kurashi/doro_kotsu/kotsu/4/14124.html'),
 ('袖ケ浦市','https://www.city.sodegaura.lg.jp/soshiki/doboku-kanri/index-2.html'),
]
CLOSE_WORDS=('通行止め','通行止','全面通行止','道路閉鎖')
RELEASE_WORDS=('解除しました','通行止め解除','通行止解除','規制解除','解除のお知らせ','解除となりました')
FUTURE_WORDS=('通行止めとなる可能性','通行止めの可能性','予定しています','通行止め予定','規制予定')
CHIBA_CODES=('E14','E82','E51','E65','C4','CA')
CHIBA_HINTS=('千葉','木更津','富津','袖ヶ浦','袖ケ浦','姉崎','市原','茂原','東金','山田','高田','蘇我','富浦','成田','大栄','松尾横芝')

KNOWN_POINTS=[
 {'match':['国道127号','元名','金谷'],'point':[139.8220,35.1570]},
 {'match':['国道16号','村田町','アンダーパス'],'point':[140.1419,35.5510]},
]

def get(url,timeout=7):
 r=requests.get(url,headers=HEADERS,timeout=timeout);r.raise_for_status();
 if not r.encoding or r.encoding.lower()=='iso-8859-1': r.encoding=r.apparent_encoding
 return r

def norm(s):return re.sub(r'\s+',' ',s or '').strip()
def hid(*p):return hashlib.sha1('|'.join(map(str,p)).encode()).hexdigest()[:14]
def txt(node):return norm(node.get_text(' ',strip=True)) if node else ''
def point_for(s):
 for x in KNOWN_POINTS:
  if all(k in s for k in x['match']): return x['point']
 return None

def current_page(text):
 # 現在性の強い表現。古い記事を大量に拾わないため、更新日が今日か「現在」「行っています」を要求。
 d1=TODAY.strftime('%Y年%-m月%-d日') if hasattr(TODAY,'strftime') else ''
 d2=TODAY.strftime('%Y年%m月%d日')
 return (d1 in text or d2 in text or '現在' in text or '行っています' in text or 'となっております' in text or '実施中' in text)

def add(items,source,source_name,kind,title,road='',location='',desc='',query='',point=None,endpoints=None):
 blob=' '.join([title,desc,location])
 if any(w in blob for w in RELEASE_WORDS) and '実施中' not in blob:return
 if any(w in blob for w in FUTURE_WORDS):return
 items.append({'id':hid(kind,title,location,source),'kind':kind,'status':'closed','title':norm(title),'road':norm(road),'location':norm(location),'description':norm(desc)[:520],'query':norm(query),'source':source,'source_name':source_name,'point':point,'endpoints':endpoints,'precision':'section_endpoints' if endpoints else ('official_point' if point else 'text_location')})

def section_between_headings(soup,needle):
 heads=soup.find_all(['h1','h2','h3','h4'])
 for h in heads:
  if needle in txt(h):
   nodes=[]
   for el in h.next_elements:
    if el is h:continue
    if getattr(el,'name',None) in ['h1','h2','h3','h4']:break
    nodes.append(el)
   return nodes
 return []

def parse_nexco(html,url,items):
 soup=BeautifulSoup(html,'html.parser'); full=txt(soup)
 if '通行止め実施中' not in full:return 0
 # 「通行止め実施中」の見出しから次見出しまでのtableだけを見る。
 h=None
 for hh in soup.find_all(['h1','h2','h3','h4']):
  if '通行止め実施中' in txt(hh): h=hh;break
 if not h:return 0
 tables=[]
 for sib in h.next_siblings:
  if getattr(sib,'name',None) in ['h1','h2','h3','h4']:break
  if getattr(sib,'name',None)=='table':tables.append(sib)
  elif hasattr(sib,'find_all'):tables.extend(sib.find_all('table'))
 added=0
 for table in tables:
  current_road=''
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
   add(items,url,'NEXCO東日本','highway',f'{road} {sec}',road,sec,joined,'',None,[a,b]);added+=1
 return added

def parse_kimitsu(html,url,items):
 t=txt(BeautifulSoup(html,'html.parser'))
 if not current_page(t):return 0
 added=0
 pat=re.compile(r'路線名\s+(.{1,70}?)\s+場所\s+(.{1,130}?)(?=\s+(?:位置図|案内図|現場状況|道路損傷|土砂崩れ|道路冠水|市道についてのお問合せ|このページに関する))')
 for m in pat.finditer(t):
  road,loc=norm(m.group(1)),norm(m.group(2));blob=road+' '+loc
  if any(w in blob for w in RELEASE_WORDS):continue
  add(items,url,'君津市','general',f'{road} 通行止め',road,loc,loc,f'{road} {loc} 千葉県',point_for(blob));added+=1
 return added

def parse_otaki(html,url,items):
 soup=BeautifulSoup(html,'html.parser');full=txt(soup)
 if not current_page(full):return 0
 added=0
 for h in soup.find_all(['h2','h3']):
  name=txt(h)
  if not re.search(r'(国道\s*\d+号|県道\s*\d+号)',name):continue
  seg=[]
  for sib in h.next_siblings:
   if getattr(sib,'name',None) in ['h2','h3']:break
   if hasattr(sib,'get_text') and txt(sib):seg.append(txt(sib))
  desc=norm(' '.join(seg));blob=name+' '+desc
  if not any(w in blob for w in CLOSE_WORDS) or any(w in blob for w in RELEASE_WORDS) or any(w in blob for w in FUTURE_WORDS):continue
  # 「～」で実区間が書かれている時だけ線候補
  eps=None
  mm=re.search(r'([^。、]{2,35})[～〜]([^。、]{2,35})',desc)
  if mm:eps=[norm(mm.group(1)),norm(mm.group(2))]
  add(items,url,'大多喜町','general',f'{name} 通行止め',name,'大多喜町',desc,f'{name} 大多喜町 千葉県',None,eps);added+=1
 return added

def parse_kisarazu(html,url,items):
 soup=BeautifulSoup(html,'html.parser');full=txt(soup)
 if not current_page(full):return 0
 added=0
 for h in soup.find_all(['h2','h3']):
  title=txt(h)
  if '通行止' not in title:continue
  if any(w in title for w in RELEASE_WORDS):continue
  road=title.replace('通行止めのお知らせ','').strip('（）() ')
  if not road:road=title
  add(items,url,'木更津市','general',title,road,'木更津市',title,f'{road} 木更津市 千葉県');added+=1
 return added

def parse_generic_current(html,url,source_name,items):
 soup=BeautifulSoup(html,'html.parser');full=txt(soup)
 if not current_page(full):return 0
 added=0
 # 「通行止めを行っています」「通行止めとなっております」等、現在形が含まれる段落だけ。
 for p in soup.find_all(['p','li','h2','h3','h4']):
  s=txt(p)
  if not any(x in s for x in ('通行止めを行っています','通行止めとなっております','全面通行止め','現在通行止め','通行止め実施中')):continue
  if any(w in s for w in RELEASE_WORDS+FUTURE_WORDS):continue
  roadm=re.search(r'((?:国道|県道|市道|林道)\s*[0-9一-龥ぁ-んァ-ヶ・－ー]+(?:号|線)?)',s);road=norm(roadm.group(1)) if roadm else ''
  locm=re.search(r'((?:[一-龥ぁ-んァ-ヶ]+市|[一-龥ぁ-んァ-ヶ]+町)[^。]{0,50}?(?:地先|付近|隧道|トンネル|橋))',s);loc=norm(locm.group(1)) if locm else ''
  if not road and not loc:continue
  eps=None;mm=re.search(r'([^。、]{2,35})[～〜]([^。、]{2,35})',s)
  if mm:eps=[norm(mm.group(1)),norm(mm.group(2))]
  add(items,url,source_name,'general',f'{road or loc} 通行止め',road,loc,s,' '.join(x for x in [road,loc,source_name,'千葉県'] if x),point_for(s),eps);added+=1
 return added

def candidate_links(html,base,source_name):
 soup=BeautifulSoup(html,'html.parser');host=urlparse(base).netloc;out=[]
 # 最新・緊急の通行止め記事だけ少数追跡
 for a in soup.find_all('a',href=True):
  label=txt(a);href=urljoin(base,a['href']);combo=label+' '+href
  if urlparse(href).netloc!=host:continue
  if not any(k in combo for k in ('通行止','道路','大雨','台風','災害','緊急')):continue
  if any(k in combo for k in ('過去','アーカイブ')):continue
  out.append(href)
 return list(dict.fromkeys(out))[:6]

def collect():
 pages=[];seen=set()
 def fetch(pair):
  n,u=pair
  try:return n,u,get(u).text,None
  except Exception as e:return n,u,None,e
 with ThreadPoolExecutor(max_workers=6) as ex:
  for f in as_completed([ex.submit(fetch,x) for x in SEEDS]):
   n,u,h,e=f.result()
   if e:print('seed',n,e);continue
   pages.append((n,u,h));seen.add(u)
   # NEXCO/袖ケ浦など一覧型だけリンクを追う
   if n in ('NEXCO東日本','袖ケ浦市'):
    for x in candidate_links(h,u,n):
     if x not in seen:seen.add(x);pages.append((n,x,None))
 # 未取得のみ並列
 need=[x for x in pages if x[2] is None];base=[x for x in pages if x[2] is not None]
 def fetchrow(row):
  n,u,_=row
  try:return n,u,get(u).text,None
  except Exception as e:return n,u,None,e
 with ThreadPoolExecutor(max_workers=8) as ex:
  for f in as_completed([ex.submit(fetchrow,x) for x in need]):
   n,u,h,e=f.result()
   if e:print('page',n,u,e);continue
   base.append((n,u,h))
 items=[]
 for n,u,h in base:
  host=urlparse(u).netloc
  try:
   if 'e-nexco.co.jp' in host:parse_nexco(h,u,items)
   elif 'city.kimitsu.lg.jp' in host:parse_kimitsu(h,u,items)
   elif 'town.otaki.chiba.jp' in host:parse_otaki(h,u,items)
   elif 'city.kisarazu.lg.jp' in host:parse_kisarazu(h,u,items)
   else:parse_generic_current(h,u,n,items)
  except Exception as e:print('parse',n,u,e)
 return items

def dedupe(xs):
 out=[];seen=set()
 for x in xs:
  k=(x['kind'],re.sub(r'\s+','',x['title']),re.sub(r'\s+','',x.get('location','')))
  if k in seen:continue
  seen.add(k);out.append(x)
 return out

def main():
 items=dedupe(collect());general=[x for x in items if x['kind']=='general'];high=[x for x in items if x['kind']=='highway']
 data={'generated_at':datetime.now(JST).isoformat(timespec='seconds'),'area':'房総半島・千葉県・東京湾アクアライン周辺','count':len(items),'general_count':len(general),'highway_count':len(high),'closures':items,'note':'現在形の通行止め情報だけを優先。NEXCOは「通行止め実施中」欄のみ。区間が特定できる規制は道路ネットワーク上で線表示するため endpoints を出力。'}
 out=Path(__file__).resolve().parent/'docs'/'closures.json';out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');print('saved',len(general),len(high),out)
if __name__=='__main__':main()
