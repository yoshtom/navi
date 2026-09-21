import json, re, math, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 BosoRoadNavi/0.3'
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.8,en;q=0.6',
}

# Official starting points. We only follow same-domain links and inspect pages that mention closure-related terms.
SEEDS = [
    'https://www.city.futtsu.lg.jp/emergencyinfo/',
    'https://www.pref.chiba.lg.jp/doukan/press/index.html',
    'https://www.ktr.mlit.go.jp/chiba/chiba_index.html',
]

# Known regulated sections used only to turn official text reports into a precise avoid-area.
# These are road-management reference sections, not assumptions that they are currently closed.
KNOWN_SECTIONS = [
    {
        'id':'r127-motona-kanaya',
        'road':'国道127号',
        'names':['元名','金谷','元名第二トンネル','明鐘トンネル'],
        'title':'国道127号 元名〜金谷',
        # Approximate centerline of the 1.5 km regulated section. Used only when an official page says this section is closed.
        'geometry': [[139.8198,35.1487],[139.8207,35.1534],[139.8229,35.1588],[139.8263,35.1638]],
        'reference':'https://www.ktr.mlit.go.jp/chiba/chiba00048.html'
    },
    {
        'id':'r127-minamimudani-koura',
        'road':'国道127号',
        'names':['南無谷','小浦','南無谷トンネル','高崎トンネル'],
        'title':'国道127号 南無谷〜小浦',
        'geometry': [[139.8320,35.0520],[139.8350,35.0600],[139.8390,35.0690],[139.8420,35.0770]],
        'reference':'https://www.ktr.mlit.go.jp/chiba/chiba00048.html'
    },
]

CLOSURE_WORDS = ['全面通行止', '通行止め', '通行止', '道路閉鎖']
RELEASE_WORDS = ['通行止め解除', '通行止解除', '規制解除', '全面通行止めを解除', '通行可能となりました']
ROAD_RE = re.compile(r'(国道\s*\d+号|県道\s*[^\s、。]{1,18}(?:線|号)|主要地方道\s*[^\s、。]{1,18})')


def get(url, timeout=18):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == 'iso-8859-1':
        r.encoding = r.apparent_encoding
    return r


def clean_text(html):
    s = BeautifulSoup(html, 'html.parser')
    for t in s(['script','style','noscript']):
        t.decompose()
    return re.sub(r'\s+', ' ', s.get_text(' ', strip=True))


def candidate_links(seed_html, seed_url):
    soup = BeautifulSoup(seed_html, 'html.parser')
    host = urlparse(seed_url).netloc
    out=[]
    for a in soup.find_all('a', href=True):
        label = re.sub(r'\s+',' ',a.get_text(' ', strip=True))
        href = urljoin(seed_url, a['href'])
        if urlparse(href).netloc != host: continue
        key = label + ' ' + href
        if any(w in key for w in ['通行止','道路','災害','緊急','規制','press','emergency']):
            out.append(href)
    # newest-ish pages are usually first; cap to avoid hammering official sites
    return list(dict.fromkeys(out))[:35]


def is_active_closure(text):
    if not any(w in text for w in CLOSURE_WORDS):
        return False
    # If release wording appears very close to the closure wording, treat as released.
    for rw in RELEASE_WORDS:
        if rw in text:
            return False
    return True


def match_known_section(text):
    best=None; score=0
    for s in KNOWN_SECTIONS:
        sc=sum(1 for n in s['names'] if n in text)
        if s['road'] in text: sc += 1
        if sc>score:
            score=sc; best=s
    return best if score>=2 else None


def extract_title(text):
    road = ROAD_RE.search(text)
    road = road.group(1).replace(' ','') if road else '道路'
    # Prefer a short phrase around the first closure keyword.
    pos=min([text.find(w) for w in CLOSURE_WORDS if text.find(w)>=0] or [0])
    chunk=text[max(0,pos-70):pos+90]
    chunk=re.sub(r'\s+',' ',chunk).strip(' ・｜|')
    return f'{road} 通行止め', chunk[:180]


def crawl_official():
    seen=set(); findings=[]
    urls=[]
    for seed in SEEDS:
        try:
            r=get(seed); urls.append(seed); urls.extend(candidate_links(r.text, seed))
        except Exception as e:
            print('seed error', seed, e)
    # Always include the currently known Futtsu emergency item as a high-signal source.
    urls.append('https://www.city.futtsu.lg.jp/emergencyinfo/0000000500.html')
    for url in list(dict.fromkeys(urls))[:90]:
        if url in seen: continue
        seen.add(url)
        try:
            r=get(url); text=clean_text(r.text)
        except Exception as e:
            print('page error', url, e); continue
        if not is_active_closure(text):
            continue
        sec=match_known_section(text)
        title,desc=extract_title(text)
        if sec:
            findings.append({
                'id':sec['id'], 'title':sec['title'], 'road':sec['road'],
                'description':desc, 'source':url, 'source_type':'official',
                'geometry':sec['geometry'], 'precision':'known_section'
            })
        else:
            # Text-only finding: show it to users, but do not create an avoid polygon without trustworthy coordinates.
            findings.append({
                'id':'text-'+str(abs(hash(url+title))), 'title':title, 'road':'',
                'description':desc, 'source':url, 'source_type':'official',
                'geometry':None, 'precision':'text_only'
            })
        time.sleep(0.25)
    return findings


def dedupe(items):
    out=[]; seen=set()
    for x in items:
        k=x['id']
        if k in seen: continue
        seen.add(k); out.append(x)
    return out


def main():
    items=dedupe(crawl_official())
    data={
        'generated_at':datetime.now(JST).isoformat(timespec='seconds'),
        'area':'房総半島・東京湾アクアライン周辺',
        'count':len(items),
        'closures':items,
        'note':'公式公開ページを自動巡回。座標を確定できた規制のみルート回避に使用します。'
    }
    out_path = Path(__file__).resolve().parent / 'docs' / 'closures.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print('saved:', out_path)
    print(json.dumps(data,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
