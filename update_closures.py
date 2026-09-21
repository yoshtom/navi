import json, re, time, hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 BosoRoadNavi/0.6'
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.8,en;q=0.6',
}

# 房総・千葉県内の道路情報を掲載する公式ページ／カテゴリ。
SEEDS = [
    ('国交省関東地整', 'https://www.ktr.mlit.go.jp/'),
    ('NEXCO東日本', 'https://www.e-nexco.co.jp/news/'),
    ('君津市', 'https://www.city.kimitsu.lg.jp/soshiki/29/68941.html'),
    ('木更津市', 'https://www.city.kisarazu.lg.jp/kurashi/doro_kotsu/kotsu/4/14124.html'),
    ('大多喜町', 'https://www.town.otaki.chiba.jp/kinkyu/2890.html'),
    ('袖ケ浦市', 'https://www.city.sodegaura.lg.jp/soshiki/doboku-kanri/index-2.html'),
    ('富津市', 'https://www.city.futtsu.lg.jp/'),
    ('鴨川市', 'https://www.city.kamogawa.lg.jp/life/1/9/'),
    ('千葉市防災', 'https://city-chiba.my.site.com/'),
]

CLOSURE_WORDS = ['通行止め','通行止','全面通行止','道路閉鎖','通行規制']
RELEASE_WORDS = ['通行止め解除','通行止解除','規制解除','通行止めを解除','通行止を解除','解除しました','解除のお知らせ']
CHIBA_HIGHWAY_CODES = ('E14','E82','E51','E65','C4','CA')
CHIBA_IC_HINTS = ('千葉','木更津','富津','袖ヶ浦','袖ケ浦','姉崎','市原','茂原','東金','山田','高田','蘇我','富浦','成田','大栄','松尾横芝')

# 正確な線を描く代わりに、既知区間は道路上の代表点だけ持つ。
KNOWN_ANCHORS = [
    {
        'match':['国道127号','元名','金谷'],
        'point':[139.8220,35.1570],
        'label':'国道127号 元名〜金谷（代表点）'
    },
    {
        'match':['国道16号','村田町','アンダーパス'],
        'point':[140.1419,35.5510],
        'label':'国道16号 村田町アンダーパス（代表点）'
    },
]


def get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == 'iso-8859-1':
        r.encoding = r.apparent_encoding
    return r


def norm(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def text_of(html):
    s = BeautifulSoup(html, 'html.parser')
    for t in s(['script','style','noscript']): t.decompose()
    return norm(s.get_text(' ', strip=True))


def active_text(t):
    if not any(w in t for w in CLOSURE_WORDS): return False
    # 「解除」だけのページは除外。実施中と解除が同居するNEXCOは専用処理する。
    if any(w in t for w in RELEASE_WORDS) and '通行止め実施中' not in t: return False
    return True


def hid(*parts):
    return hashlib.sha1('|'.join(map(str,parts)).encode('utf-8')).hexdigest()[:14]


def anchor_for(text):
    for a in KNOWN_ANCHORS:
        if all(x in text for x in a['match']):
            return a['point']
    return None


def add_item(items, *, title, source, source_name, kind='general', road='', location='', query='', description='', point=None, endpoints=None, status='closed'):
    title=norm(title); description=norm(description); road=norm(road); location=norm(location); query=norm(query)
    if not title: return
    key = f'{kind}|{road}|{location}|{title}|{source}'
    items.append({
        'id': hid(key), 'kind':kind, 'status':status,
        'title':title, 'road':road, 'location':location,
        'query':query, 'description':description[:420],
        'source':source, 'source_name':source_name,
        'point':point, 'endpoints':endpoints,
        'precision':'official_point' if point else ('section_endpoints' if endpoints else 'text_location')
    })


def same_domain_links(html, base, keywords, cap=50):
    soup=BeautifulSoup(html,'html.parser'); host=urlparse(base).netloc; out=[]
    for a in soup.find_all('a',href=True):
        label=norm(a.get_text(' ',strip=True)); href=urljoin(base,a['href'])
        if urlparse(href).netloc != host: continue
        if any(k in (label+' '+href) for k in keywords): out.append(href)
    return list(dict.fromkeys(out))[:cap]


def parse_nexco(html, url, items):
    soup=BeautifulSoup(html,'html.parser')
    full=norm(soup.get_text(' ',strip=True))
    if '通行止め実施中' not in full: return 0
    added=0
    for table in soup.find_all('table'):
        rows=table.find_all('tr'); current_road=''
        for tr in rows:
            cells=[norm(x.get_text(' ',strip=True)) for x in tr.find_all(['th','td'])]
            if len(cells)<2: continue
            joined=' | '.join(cells)
            # road name may be omitted on continuation rows
            first=cells[0]
            if any(code in first for code in CHIBA_HIGHWAY_CODES) or '自動車道' in first or '道路' in first:
                current_road=first
            road=current_road
            if not road: continue
            if not (any(code in road for code in CHIBA_HIGHWAY_CODES) or any(h in joined for h in CHIBA_IC_HINTS)): continue
            # find section cell containing ～ / ⇔
            sec=''
            for c in cells:
                if ('～' in c or '⇔' in c or '〜' in c) and ('IC' in c or 'JCT' in c): sec=c; break
            if not sec: continue
            parts=re.split(r'～|⇔|〜',sec)
            if len(parts)<2: continue
            a,b=norm(parts[0]),norm(parts[1])
            add_item(items,title=f'{road} {sec}',source=url,source_name='NEXCO東日本',kind='highway',road=road,location=sec,
                     description=joined,query='',endpoints=[a,b])
            added+=1
    return added


def parse_kimitsu(html,url,items):
    t=text_of(html); added=0
    # 路線名 + 場所 の組
    pat=re.compile(r'路線名\s+(.{1,60}?)\s+場所\s+(.{1,100}?)(?=\s+(?:位置図|案内図|現場状況|道路損傷|土砂崩れ|道路冠水|隧道|市道についてのお問合せ|このページに関する))')
    for m in pat.finditer(t):
        road,loc=norm(m.group(1)),norm(m.group(2))
        if any(x in loc for x in RELEASE_WORDS): continue
        q=f'{road} {loc} 千葉県'
        add_item(items,title=f'{road} 通行止め',source=url,source_name='君津市',road=road,location=loc,query=q,description=loc,point=anchor_for(road+' '+loc));added+=1
    # 路線名なしの冠水等
    for m in re.finditer(r'(君津市[^。]{2,70}?(?:隧道|地先|付近))\s*通行止め',t):
        loc=norm(m.group(1))
        if any(x['location']==loc for x in items): continue
        add_item(items,title=f'{loc} 通行止め',source=url,source_name='君津市',location=loc,query=loc+' 千葉県',description=loc);added+=1
    return added


def parse_otaki(html,url,items):
    soup=BeautifulSoup(html,'html.parser'); added=0
    for h in soup.find_all(['h2','h3']):
        name=norm(h.get_text(' ',strip=True))
        if not re.search(r'(国道\s*\d+号|県道\s*\d+号)',name): continue
        seg=[]
        for sib in h.next_siblings:
            if getattr(sib,'name',None) in ['h2','h3']: break
            txt=norm(getattr(sib,'get_text',lambda *a,**k:'')(' ',strip=True)) if hasattr(sib,'get_text') else ''
            if txt: seg.append(txt)
        desc=norm(' '.join(seg))
        if '通行止' not in desc or any(w in desc for w in RELEASE_WORDS): continue
        add_item(items,title=f'{name} 通行止め',source=url,source_name='大多喜町',road=name,location='大多喜町',query=f'{name} 大多喜町 千葉県',description=desc);added+=1
    return added


def parse_kisarazu(html,url,items):
    soup=BeautifulSoup(html,'html.parser');added=0
    for h in soup.find_all(['h2','h3']):
        tx=norm(h.get_text(' ',strip=True))
        m=re.search(r'通行止めのお知らせ[（(](.+?)[）)]',tx)
        if not m: continue
        road=norm(m.group(1))
        add_item(items,title=f'{road} 通行止め',source=url,source_name='木更津市',road=road,location='木更津市',query=f'{road} 木更津市 千葉県',description=tx);added+=1
    return added


def parse_generic_sections(html,url,source_name,items):
    soup=BeautifulSoup(html,'html.parser'); added=0
    # 見出しごとのブロックを拾う。正確な位置が書かれている場合だけ query 化。
    for h in soup.find_all(['h1','h2','h3','h4']):
        title=norm(h.get_text(' ',strip=True))
        if not any(w in title for w in CLOSURE_WORDS) and not re.search(r'(国道|県道|市道|林道).{0,25}',title): continue
        seg=[]
        for sib in h.next_siblings:
            if getattr(sib,'name',None) in ['h1','h2','h3','h4']: break
            if hasattr(sib,'get_text'):
                tx=norm(sib.get_text(' ',strip=True))
                if tx: seg.append(tx)
        desc=norm(' '.join(seg))[:600]
        blob=title+' '+desc
        if not active_text(blob): continue
        if any(w in blob for w in RELEASE_WORDS): continue
        roadm=re.search(r'((?:国道|県道|市道|林道)\s*[0-9一-龥ぁ-んァ-ヶ・－ー]+(?:号|線)?)',blob)
        road=norm(roadm.group(1)) if roadm else ''
        locm=re.search(r'((?:千葉県)?(?:[一-龥ぁ-んァ-ヶ]+市|[一-龥ぁ-んァ-ヶ]+町)[^。]{0,45}?(?:地先|付近|隧道|トンネル|橋))',blob)
        loc=norm(locm.group(1)) if locm else ''
        q=' '.join(x for x in [road,loc,source_name,'千葉県'] if x)
        add_item(items,title=title if '通行' in title else f'{title} 通行止め',source=url,source_name=source_name,road=road,location=loc,query=q,description=desc,point=anchor_for(blob));added+=1
    return added


def parse_ktr_page(html,url,items):
    soup=BeautifulSoup(html,'html.parser'); title=norm(soup.title.get_text(' ',strip=True) if soup.title else '')
    t=norm(soup.get_text(' ',strip=True))
    if '通行止' not in (title+' '+t) or any(w in title for w in RELEASE_WORDS): return 0
    # プレス本文がPDFのみでもタイトルから拾う
    m=re.search(r'～?\s*((?:国道\s*\d+号)[^～｜|]{0,60})',title+' '+t)
    roadloc=norm(m.group(1)) if m else title
    if '千葉' not in t and not any(x in roadloc for x in ['国道16号','国道127号','国道357号']): return 0
    roadm=re.search(r'(国道\s*\d+号)',roadloc);road=roadm.group(1) if roadm else ''
    q=roadloc+' 千葉県'
    add_item(items,title=roadloc+' 通行止め' if '通行止' not in roadloc else roadloc,source=url,source_name='国交省関東地方整備局',road=road,location=roadloc,query=q,description=title,point=anchor_for(roadloc+' '+t))
    return 1


def collect():
    items=[]; seen_urls=set(); queue=[]
    # まずシードを取得し、同一ドメインの通行止めリンクを追加
    for source_name,url in SEEDS:
        try:
            r=get(url); queue.append((source_name,url,r.text))
            links=same_domain_links(r.text,url,['通行止','通行規制','道路','災害','重要なお知らせ','緊急'],cap=45)
            for href in links: queue.append((source_name,href,None))
        except Exception as e: print('seed error',source_name,url,e)
    # 現在情報の高シグナルURL
    queue.extend([
        ('NEXCO東日本','https://www.e-nexco.co.jp/news/important_info/result.php',None),
        ('富津市','https://www.city.futtsu.lg.jp/emergencyinfo/0000000500.html',None),
    ])

    # NEXCO一覧から最新記事をさらに拾う
    expanded=[]
    for source_name,url,html in queue:
        if url in seen_urls: continue
        seen_urls.add(url)
        try:
            if html is None: html=get(url).text
        except Exception as e:
            print('page error',source_name,url,e); continue
        expanded.append((source_name,url,html))
        if 'e-nexco.co.jp' in urlparse(url).netloc and ('/news/' in url or 'result.php' in url):
            for href in same_domain_links(html,url,['通行止め','台風','大雨'],cap=20):
                if href not in seen_urls: queue.append(('NEXCO東日本',href,None))
        time.sleep(0.15)

    for source_name,url,html in expanded:
        host=urlparse(url).netloc
        try:
            if 'e-nexco.co.jp' in host:
                parse_nexco(html,url,items); continue
            if 'city.kimitsu.lg.jp' in host and '68941' in url:
                parse_kimitsu(html,url,items); continue
            if 'city.kisarazu.lg.jp' in host and '14124' in url:
                parse_kisarazu(html,url,items); continue
            if 'town.otaki.chiba.jp' in host and '2890' in url:
                parse_otaki(html,url,items); continue
            if 'ktr.mlit.go.jp' in host:
                parse_ktr_page(html,url,items); continue
            parse_generic_sections(html,url,source_name,items)
        except Exception as e:
            print('parse error',source_name,url,e)
    return items


def dedupe(items):
    out=[]; seen=set()
    for x in items:
        # title+location+kind で重複除外。同じ規制を複数ソースが報じた場合は最初を残す。
        k=(x['kind'],re.sub(r'\s+','',x['title']),re.sub(r'\s+','',x.get('location','')))
        if k in seen: continue
        seen.add(k); out.append(x)
    return out


def main():
    items=dedupe(collect())
    general=[x for x in items if x['kind']=='general']
    highway=[x for x in items if x['kind']=='highway']
    data={
        'generated_at':datetime.now(JST).isoformat(timespec='seconds'),
        'area':'房総半島・千葉県・東京湾アクアライン周辺',
        'count':len(items),'general_count':len(general),'highway_count':len(highway),
        'closures':items,
        'note':'公式ページを自動巡回。道路形状が確定できない規制は偽の線を描かず、地点として表示・回避します。高速道路はNEXCO東日本の通行止め実施中区間を区間単位で取得します。'
    }
    out=Path(__file__).resolve().parent/'docs'/'closures.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print('saved',out,'general',len(general),'highway',len(highway),'total',len(items))
    print(json.dumps(data,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
