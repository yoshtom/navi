import json,re,hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

JST=timezone(timedelta(hours=9)); NOW=datetime.now(JST)
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 BosoRoadNavi/1.0','Accept-Language':'ja,en-US;q=0.8'}

# 固定マスター: 推測ジオコーディングをしないための房総主要IC/JCT座標 [lon,lat]
IC={
 '穴川IC':[140.1141,35.64176],
 '蘇我IC':[140.1378,35.56703],
 '市原IC':[140.0935,35.49623],
 '姉崎袖ヶ浦IC':[140.04269,35.43025],
 '木更津北IC':[139.9977,35.38604],
 '木更津JCT':[139.9799,35.37643],
 '木更津南JCT':[139.9485,35.35395],
 '木更津南IC':[139.9206,35.35032],
 '富津竹岡IC':[139.8598024,35.1982086],
 '富浦IC':[139.8506,35.03824],
 '袖ケ浦IC':[139.9624,35.40812],
 '川崎浮島JCT':[139.7876,35.52065],
}
# NEXCOで使われる表記揺れ
ALIASES={'袖ヶ浦IC':'袖ケ浦IC','東金IC・JCT':'東金JCT','千葉東JCT':'千葉東JCT'}

# 元名区間は富津市の当日緊急情報を直接監視。表示用に代表点を複数持つ。
# これは「道路形状」ではなく、Mapbox driving でこの両端を結んで国道127号形状を取得するためのアンカー。
KNOWN_GENERAL={
 'motona127':{
   'source_name':'富津市','source':'https://www.city.futtsu.lg.jp/emergencyinfo/0000000500.html',
   'title':'国道127号 金谷〜元名 通行止め','road':'国道127号','location':'富津市金谷地先〜鋸南町元名地先 約1.5km',
   # 明鐘トンネル木更津側（不動岩）〜元名第二トンネル館山側の近傍アンカー
   'fixed_points':[[139.8204,35.1725],[139.8219,35.1592]],
 }
}

def get(url,timeout=10):
 r=requests.get(url,headers=HEADERS,timeout=timeout); r.raise_for_status()
 if not r.encoding or r.encoding.lower()=='iso-8859-1': r.encoding=r.apparent_encoding
 return r

def norm(s): return re.sub(r'\s+',' ',s or '').strip()
def txt(n): return norm(n.get_text(' ',strip=True)) if n else ''
def hid(*p): return hashlib.sha1('|'.join(map(str,p)).encode()).hexdigest()[:14]

def current_motona():
 try:
  t=txt(BeautifulSoup(get(KNOWN_GENERAL['motona127']['source']).text,'html.parser'))
  # 今日の緊急情報で、規制区間と通り抜け不可が明記されている間だけ有効
  return ('国道127号' in t and '元名' in t and '金谷' in t and ('通り抜けができません' in t or '通行止め' in t or '事前通行規制' in t))
 except Exception as e:
  print('motona fetch',e); return False

def latest_nexco_page():
 # 最新の「通行止め実施中」ページをNEXCO新着から探す
 urls=['https://www.e-nexco.co.jp/news/','https://www.e-nexco.co.jp/whatsnew/']
 for u in urls:
  try:
   soup=BeautifulSoup(get(u).text,'html.parser')
   for a in soup.find_all('a',href=True):
    label=txt(a)
    if '通行止め' in label and ('実施' in label or '台風' in label or '大雨' in label):
     href=urljoin(u,a['href'])
     if 'important_info' in href:
      h=get(href).text
      if '通行止め実施中の区間' in txt(BeautifulSoup(h,'html.parser')):
       return href,h
  except Exception as e: print('nexco list',u,e)
 return None,None

def parse_nexco_current(url,html):
 soup=BeautifulSoup(html,'html.parser')
 # 表がHTML tableなら直接解析
 items=[]; current_road=''; in_current=False
 # テキストを行相当にして、実施中～次セクションまで読む。サイト構造変化に強い。
 lines=[norm(x) for x in soup.stripped_strings]
 for s in lines:
  if '通行止め実施中の区間' in s: in_current=True; continue
  if in_current and ('通行止めの可能性' in s or re.match(r'^2[\.．、 ]',s) or '局地的な大雨' in s): break
  if not in_current: continue
  # 道路名を更新
  mroad=re.search(r'((?:C4|E14|E82|E51|E65|CA)\s*[^|]{0,40}?(?:自動車道|道路|連絡道))',s)
  if mroad: current_road=norm(mroad.group(1))
  # 区間抽出（JCT/IC、穴川など）
  m=re.search(r'([^|、]{1,25}(?:IC|JCT|穴川))\s*[～〜]\s*([^|、]{1,25}(?:IC|JCT))',s)
  if not m: continue
  a,b=norm(m.group(1)),norm(m.group(2))
  a=ALIASES.get(a,a); b=ALIASES.get(b,b)
  # 房総・千葉関連のみ
  if not any(k in (current_road+' '+a+' '+b) for k in ['E14','E82','C4','CA','E51','E65','木更津','富津','袖','千葉','東金','茂原','成田']): continue
  fp=[]
  if a in IC: fp.append(IC[a])
  if b in IC: fp.append(IC[b])
  items.append({
   'id':hid('highway',current_road,a,b),'kind':'highway','status':'closed','title':f'{current_road or "高速道路"} {a}〜{b}',
   'road':current_road,'location':f'{a}〜{b}','description':'NEXCO東日本「通行止め実施中」から取得',
   'source':url,'source_name':'NEXCO東日本','endpoints':[a,b], 'fixed_points':fp if len(fp)==2 else None,
   'precision':'fixed_ic_master' if len(fp)==2 else 'endpoint_text'
  })
 return dedupe(items)

def dedupe(xs):
 out=[]; seen=set()
 for x in xs:
  k=(x['kind'],x['title'])
  if k in seen: continue
  seen.add(k); out.append(x)
 return out

def main():
 items=[]
 if current_motona():
  g=KNOWN_GENERAL['motona127'].copy(); g.update({'id':'motona127','kind':'general','status':'closed','description':'富津市の当日緊急情報から取得','precision':'fixed_section_anchor'})
  items.append(g)
 url,html=latest_nexco_page()
 if url and html: items.extend(parse_nexco_current(url,html))
 else: print('no current NEXCO page')
 data={
  'generated_at':datetime.now(JST).isoformat(timespec='seconds'), 'area':'房総半島・千葉県・東京湾アクアライン周辺',
  'count':len(items),'general_count':sum(x['kind']=='general' for x in items),'highway_count':sum(x['kind']=='highway' for x in items),
  'closures':items,
  'note':'v6: 高速はNEXCO最新の「通行止め実施中」だけを取得。主要IC/JCTは固定座標マスター。元名127号は富津市の当日緊急情報を直接監視。表示線は交通非考慮の道路形状、ナビは交通考慮＋強制除外。'
 }
 out=Path(__file__).resolve().parent/'docs'/'closures.json'; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8'); print('saved',data['general_count'],data['highway_count'],out)
if __name__=='__main__': main()
