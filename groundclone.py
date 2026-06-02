#!/usr/bin/env python3
"""
BiasFeed - a personal, local political-news bias dashboard, templated on Ground News.

Pipeline:
  read sources.csv -> fetch RSS -> cluster into stories -> keep POLITICAL ones
  (editable keyword buckets, strict/loose toggle) -> bias bar per story
  -> Blindspot formula (faithful to Ground News, scaled to our pool)
  -> news.html: a filterable, sortable dashboard.

Layout: featured cards (5+ sources; top 2 = hero) over a compact "More Coverage"
list (<5 sources). A sticky bar filters by lean tilt and topic, and sorts by
coverage or lopsidedness. Blindspots wear a flag on their card.

Only dependency: feedparser.  Everything runs locally.

Usage:
    python groundclone.py
    python groundclone.py --scope strict
    python groundclone.py --max-age 2 --sim 0.18
"""

import argparse
import csv
import html
import math
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

try:
    import feedparser
except ImportError:
    sys.exit("Missing dependency. Run:  pip install feedparser")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
feedparser.USER_AGENT = USER_AGENT
FETCH_TIMEOUT = 15
socket.setdefaulttimeout(FETCH_TIMEOUT)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SIM_THRESHOLD = 0.20
MIN_CLUSTER_SOURCES = 2
MIN_FEATURED_SOURCES = 5      # below this, a story goes to "More Coverage", not a card
MAX_AGE_DAYS = 3
PER_FEED_LIMIT = 60
POLITICAL_SCOPE = "loose"
TILT_MARGIN = 25             # |Right% - Left%| >= this -> story "leans" that way (tune to taste)

BUCKET = {"far-left": "L", "left": "L", "lean-left": "L", "center": "C",
          "lean-right": "R", "right": "R", "far-right": "R"}

QUIET_SIDE_MAX_SOURCES = 3
BLINDSPOT_MIN_OTHER_PCT = 33
BLINDSPOT_COEF = 30 / 37
BLINDSPOT_MAX_LOW_FACT_PCT = 35

STOPWORDS = set("""
a an the and or but if then else for to of in on at by with from as is are was were be been being
this that these those it its it's he she they them his her their our your my we you i me us not no
will would can could should may might must do does did has have had over under after before about into
out up down off than too very just so new says say said report reports new update live latest news
""".split())


# ----------------------------------------------------------------------------
# SOURCES + TOPICS
# ----------------------------------------------------------------------------
def load_sources(path):
    sources = {}
    with open(path, encoding="utf-8") as f:
        rows = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        name = r["name"].strip()
        bias = r["bias"].strip().lower()
        sources[name] = {"feed_url": r["feed_url"].strip(), "bias": bias,
                         "bucket": BUCKET.get(bias, "C"),
                         "factuality": r.get("factuality", "mixed").strip().lower()}
    return sources


def load_topics(path, scope):
    """Return [(bucket_name, tier, compiled_regex)] for the enabled tiers."""
    buckets = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "|" not in ln:
                continue
            tier, bucket, kws = (p.strip() for p in ln.split("|", 2))
            if tier == "core" or (tier == "loose" and scope == "loose"):
                words = [k.strip().lower() for k in kws.split(",") if k.strip()]
                if words:
                    pat = r"\b(" + "|".join(re.escape(w) for w in
                                            sorted(set(words), key=len, reverse=True)) + r")\b"
                    buckets.append((bucket, tier, re.compile(pat, re.IGNORECASE)))
    return buckets


def story_topics(arts, buckets):
    blob = " ".join(f"{a['title']} {a['summary']}" for a in arts)
    return [name for name, _tier, rx in buckets if rx.search(blob)]


# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------
def parse_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def fetch_articles(sources, max_age_days, per_feed_limit):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    articles = []
    for name, meta in sources.items():
        try:
            feed = feedparser.parse(meta["feed_url"], agent=USER_AGENT)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name}: fetch error ({e})", file=sys.stderr)
            continue
        if getattr(feed, "bozo", 0) and not feed.entries:
            print(f"  ! {name}: no entries (timed out or feed moved)", file=sys.stderr)
            continue
        kept = 0
        for entry in feed.entries[:per_feed_limit]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            dt = parse_date(entry)
            if dt and dt < cutoff:
                continue
            summary = re.sub("<[^>]+>", " ", entry.get("summary", ""))
            articles.append({"title": title, "link": link, "summary": summary.strip(),
                             "source": name, "bucket": meta["bucket"], "bias": meta["bias"],
                             "factuality": meta["factuality"], "date": dt})
            kept += 1
        print(f"  - {name}: {kept} articles", file=sys.stderr)
    return articles


# ----------------------------------------------------------------------------
# CLUSTER
# ----------------------------------------------------------------------------
def tokenize(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower())
            if len(w) > 2 and w not in STOPWORDS]


def tfidf_vectors(docs):
    tokenized = [tokenize(d) for d in docs]
    df = Counter()
    for toks in tokenized:
        for w in set(toks):
            df[w] += 1
    n = len(docs)
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
    vectors = []
    for toks in tokenized:
        tf = Counter(toks)
        vec = {w: (c / len(toks)) * idf[w] for w, c in tf.items()} if toks else {}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({w: v / norm for w, v in vec.items()})
    return vectors


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(w, 0.0) for w, v in a.items())


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def cluster(articles, threshold):
    docs = [f"{a['title']} {a['summary']}" for a in articles]
    vecs = tfidf_vectors(docs)
    term_docs = defaultdict(list)
    for i, v in enumerate(vecs):
        for w in v:
            term_docs[w].append(i)
    uf = UnionFind(len(articles))
    checked = set()
    for w, ids in term_docs.items():
        if len(ids) > 400:
            continue
        for ai in range(len(ids)):
            for bi in range(ai + 1, len(ids)):
                i, j = ids[ai], ids[bi]
                if (i, j) in checked:
                    continue
                checked.add((i, j))
                if cosine(vecs[i], vecs[j]) >= threshold:
                    uf.union(i, j)
    groups = defaultdict(list)
    for i in range(len(articles)):
        groups[uf.find(i)].append(articles[i])
    return list(groups.values())


# ----------------------------------------------------------------------------
# SUMMARIZE
# ----------------------------------------------------------------------------
def summarize_story(arts):
    by_source = {}
    for a in arts:
        by_source.setdefault(a["source"], a)
    sources = list(by_source.values())
    counts = Counter(s["bucket"] for s in sources)
    total = len(sources)
    pct = {b: round(100 * counts.get(b, 0) / total) for b in ("L", "C", "R")}
    low_fact_pct = 100 * sum(1 for s in sources if s["factuality"] == "low") / total

    blindspot = None
    for side, other in (("L", "R"), ("R", "L")):
        side_n, side_pct, other_pct = counts.get(side, 0), pct[side], pct[other]
        if (side_n < QUIET_SIDE_MAX_SOURCES
                and other_pct >= BLINDSPOT_MIN_OTHER_PCT
                and side_pct <= max(0, (other_pct - BLINDSPOT_MIN_OTHER_PCT)) * BLINDSPOT_COEF
                and low_fact_pct <= BLINDSPOT_MAX_LOW_FACT_PCT):
            blindspot = "Left" if side == "L" else "Right"
            break

    rep = sorted(arts, key=lambda a: (
        0 if a["bucket"] == "C" else 1,
        0 if a["factuality"] == "high" else 1,
        -(a["date"].timestamp() if a["date"] else 0)))[0]
    return {"headline": rep["title"], "link": rep["link"], "total": total, "pct": pct,
            "counts": {b: counts.get(b, 0) for b in ("L", "C", "R")},
            "blindspot": blindspot, "topics": [],
            "sources": sorted(sources, key=lambda s: s["source"]),
            "date": max((a["date"] for a in arts if a["date"]), default=None)}


# ----------------------------------------------------------------------------
# DERIVED HELPERS
# ----------------------------------------------------------------------------
def tilt(s):
    d = s["pct"]["R"] - s["pct"]["L"]
    if d >= TILT_MARGIN:
        return "right"
    if d <= -TILT_MARGIN:
        return "left"
    return "balanced"


def tilt_label(t):
    return {"left": "Leans Left", "right": "Leans Right", "balanced": "Balanced"}[t]


def lop(s):
    return abs(s["pct"]["R"] - s["pct"]["L"])


def pretty(name):
    return name.replace("-", " ").title()


def date_str(s):
    return s["date"].strftime("%b %d, %H:%M UTC") if s["date"] else ""


# ----------------------------------------------------------------------------
# RENDER
# ----------------------------------------------------------------------------
def bar(pct, mini=False):
    seg = ""
    for b, cls in (("L", "l"), ("C", "c"), ("R", "r")):
        if pct[b] > 0:
            label = "" if mini else f"{pct[b]}%"
            seg += f'<span class="seg {cls}" style="width:{pct[b]}%">{label}</span>'
    return f'<div class="bar{" mini" if mini else ""}">{seg}</div>'


def flag(side):
    return f'<span class="flag {side.lower()}">Blindspot · under-covered by the {side}</span>'


def chips(s):
    return '<div class="chips">' + "".join(
        f'<span class="chip {src["bucket"].lower()}">{html.escape(src["source"])}</span>'
        for src in s["sources"]) + '</div>'


def data_attrs(s):
    return (f'data-cov="{s["total"]}" data-lop="{lop(s)}" '
            f'data-tilt="{tilt(s)}" data-topics="{" ".join(s["topics"])}"')


def featured_card(s, hero=False):
    t = tilt(s)
    cls = "card featured " + ("hero" if hero else "mid")
    f = flag(s["blindspot"]) if s["blindspot"] else ""
    return (f'<article class="{cls}" {data_attrs(s)}>'
            f'{f}<span class="tilt {t}">{tilt_label(t)}</span>'
            f'<a class="headline" href="{html.escape(s["link"])}" target="_blank" rel="noopener">'
            f'{html.escape(s["headline"])}</a>'
            f'<div class="meta">{s["total"]} sources · {date_str(s)}</div>'
            f'{bar(s["pct"])}{chips(s)}</article>')


def list_row(s):
    dot = (f'<span class="dot {s["blindspot"].lower()}" title="Blindspot"></span>'
           if s["blindspot"] else "")
    return (f'<a class="row" href="{html.escape(s["link"])}" target="_blank" rel="noopener" {data_attrs(s)}>'
            f'<span class="rowhead">{dot}{html.escape(s["headline"])}</span>'
            f'<span class="rowbar">{bar(s["pct"], mini=True)}</span>'
            f'<span class="rowcount">{s["total"]}</span></a>')


CSS = """<style>
  :root { --paper:#f4f1ea; --ink:#1a1714; --muted:#7a736a; --line:#ddd6c9;
    --L:#2f6db4; --C:#9a9ea6; --R:#c0392b; --card:#fffdf8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
    font-family:'IBM Plex Sans',system-ui,sans-serif; line-height:1.45; }
  .wrap { max-width:1180px; margin:0 auto; padding:0 24px; }
  header { padding:36px 0 18px; }
  .wordmark { font-family:'Fraunces',serif; font-weight:900; font-size:clamp(32px,5.5vw,52px);
    letter-spacing:-.02em; line-height:.95; }
  .wordmark span { color:var(--R); }
  .tagline { font-family:'Fraunces',serif; font-style:italic; color:var(--muted);
    font-size:17px; margin-top:4px; }
  .sub { color:var(--muted); margin-top:10px; font-size:13px; }
  .legend { display:flex; gap:16px; margin-top:12px; font-size:13px; flex-wrap:wrap; }
  .legend i { width:12px; height:12px; border-radius:2px; display:inline-block;
    margin-right:6px; vertical-align:-1px; }

  /* Sticky filter bar */
  .filterbar { position:sticky; top:0; z-index:20; background:var(--paper);
    border-top:3px solid var(--ink); border-bottom:1px solid var(--line);
    padding:12px 0; margin-bottom:10px; display:flex; gap:26px; flex-wrap:wrap;
    overflow-x:auto; }
  .fgroup { display:flex; align-items:center; gap:7px; }
  .flabel { font-family:'Fraunces',serif; font-size:12px; text-transform:uppercase;
    letter-spacing:.08em; color:var(--muted); margin-right:2px; }
  .fbtn { font-family:inherit; font-size:12.5px; padding:5px 11px; border-radius:20px;
    border:1px solid var(--line); background:#fff; color:var(--ink); cursor:pointer;
    white-space:nowrap; transition:.12s; }
  .fbtn:hover { border-color:var(--ink); }
  .fbtn.active { background:var(--ink); color:#fff; border-color:var(--ink); }

  .section-label { font-family:'Fraunces',serif; font-weight:600; font-size:14px;
    text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
    margin:24px 0 14px; display:flex; align-items:center; gap:10px; }
  .section-label::after { content:""; flex:1; height:1px; background:var(--line); }

  /* Featured grid: heroes span 3 of 6 cols (2 per row), mids span 2 (3 per row) */
  #featured { display:grid; grid-template-columns:repeat(6,1fr); gap:18px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:18px; display:flex; flex-direction:column; gap:10px; }
  .card.hero { grid-column:span 3; }
  .card.mid  { grid-column:span 2; }
  .hero .headline { font-size:25px; }
  .mid .headline { font-size:18px; }
  .headline { font-family:'Fraunces',serif; font-weight:600; color:var(--ink);
    text-decoration:none; line-height:1.18; }
  .headline:hover { text-decoration:underline; }
  .meta { color:var(--muted); font-size:12.5px; }
  .tilt { align-self:flex-start; font-size:11px; font-weight:600; letter-spacing:.03em;
    padding:2px 9px; border-radius:20px; border:1px solid var(--line); color:var(--muted); }
  .tilt.left { color:var(--L); border-color:#cdddf0; background:#f3f8fd; }
  .tilt.right { color:var(--R); border-color:#f0cfca; background:#fdf4f3; }
  .bar { display:flex; height:24px; border-radius:5px; overflow:hidden; margin-top:auto;
    font-size:11px; font-weight:600; color:#fff; }
  .bar.mini { height:8px; border-radius:4px; }
  .seg { display:flex; align-items:center; justify-content:center; min-width:0; }
  .seg.l { background:var(--L); } .seg.c { background:var(--C); } .seg.r { background:var(--R); }
  .flag { align-self:flex-start; font-size:10.5px; font-weight:600; letter-spacing:.04em;
    text-transform:uppercase; padding:4px 9px; border-radius:20px; }
  .flag.left { background:#e8f0f9; color:var(--L); border:1px solid #bcd4ee; }
  .flag.right { background:#fbeae8; color:var(--R); border:1px solid #f0c4bd; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .chip { font-size:11px; padding:3px 8px; border-radius:20px; border:1px solid var(--line);
    background:#fff; color:#5b6068; }
  .chip.l { color:var(--L); border-color:#cdddf0; }
  .chip.r { color:var(--R); border-color:#f0cfca; }

  #featempty { display:none; color:var(--muted); font-size:14px; padding:8px 0; }

  /* Compact list */
  #morelist { border-top:1px solid var(--line); }
  .row { display:grid; grid-template-columns:1fr 120px 34px; align-items:center; gap:14px;
    padding:11px 6px; border-bottom:1px solid var(--line); text-decoration:none; color:var(--ink); }
  .row:hover { background:#fffaf0; }
  .rowhead { font-size:15px; display:flex; align-items:center; gap:8px; }
  .rowcount { font-size:12.5px; color:var(--muted); text-align:right; }
  .dot { width:9px; height:9px; border-radius:50%; flex:0 0 auto; }
  .dot.left { background:var(--L); } .dot.right { background:var(--R); }

  footer { color:var(--muted); font-size:12.5px; border-top:1px solid var(--line);
    margin-top:40px; padding:24px 0 60px; }
  @media (max-width:760px) {
    #featured { grid-template-columns:1fr; }
    .card.hero, .card.mid { grid-column:span 1; }
    .row { grid-template-columns:1fr 70px 28px; } .rowbar { display:none; }
  }
</style>"""

JS = """<script>
(function(){
  var state={lean:'all',topic:'all',sort:'cov'};
  var feat=Array.prototype.slice.call(document.querySelectorAll('#featured .card'));
  var rows=Array.prototype.slice.call(document.querySelectorAll('#morelist .row'));
  var fg=document.getElementById('featured');
  var ml=document.getElementById('morelist');
  function match(el){
    if(state.lean!=='all' && el.dataset.tilt!==state.lean) return false;
    if(state.topic!=='all' && el.dataset.topics.split(' ').indexOf(state.topic)<0) return false;
    return true;
  }
  function key(el){ return state.sort==='cov' ? +el.dataset.cov : +el.dataset.lop; }
  function apply(){
    var vis=feat.filter(match), hid=feat.filter(function(e){return !match(e);});
    vis.sort(function(a,b){ return key(b)-key(a) || (+b.dataset.cov)-(+a.dataset.cov); });
    vis.forEach(function(el,i){ el.style.display='';
      el.classList.toggle('hero', i<2); el.classList.toggle('mid', i>=2); fg.appendChild(el); });
    hid.forEach(function(el){ el.style.display='none'; });
    document.getElementById('featempty').style.display = vis.length?'none':'block';

    var vr=rows.filter(match), hr=rows.filter(function(e){return !match(e);});
    vr.sort(function(a,b){ return key(b)-key(a) || (+b.dataset.cov)-(+a.dataset.cov); });
    vr.forEach(function(el){ el.style.display=''; ml.appendChild(el); });
    hr.forEach(function(el){ el.style.display='none'; });
    var mw=document.getElementById('morewrap'); if(mw) mw.style.display = vr.length?'':'none';
  }
  function wire(group,field){
    var btns=document.querySelectorAll('[data-group="'+group+'"]');
    Array.prototype.forEach.call(btns,function(btn){
      btn.addEventListener('click',function(){
        Array.prototype.forEach.call(btns,function(b){ b.classList.remove('active'); });
        btn.classList.add('active'); state[field]=btn.dataset.val; apply();
      });
    });
  }
  wire('lean','lean'); wire('topic','topic'); wire('sort','sort');
  apply();
})();
</script>"""


def render_html(stories, generated_at, source_count, scope, filter_topics):
    ordered = sorted(stories, key=lambda s: -s["total"])
    featured = [s for s in ordered if s["total"] >= MIN_FEATURED_SOURCES]
    more = [s for s in ordered if s["total"] < MIN_FEATURED_SOURCES]
    blind = sum(1 for s in stories if s["blindspot"])

    tcount = Counter(tilt(s) for s in stories)
    topcount = Counter(t for s in stories for t in s["topics"] if t in filter_topics)

    lean_btns = "".join([
        '<button class="fbtn active" data-group="lean" data-val="all">All</button>',
        f'<button class="fbtn" data-group="lean" data-val="left">Leans Left ({tcount.get("left",0)})</button>',
        f'<button class="fbtn" data-group="lean" data-val="balanced">Balanced ({tcount.get("balanced",0)})</button>',
        f'<button class="fbtn" data-group="lean" data-val="right">Leans Right ({tcount.get("right",0)})</button>'])

    topic_btns = '<button class="fbtn active" data-group="topic" data-val="all">All</button>' + "".join(
        f'<button class="fbtn" data-group="topic" data-val="{t}">{pretty(t)} ({c})</button>'
        for t, c in sorted(topcount.items(), key=lambda kv: -kv[1]))

    sort_btns = ('<button class="fbtn active" data-group="sort" data-val="cov">Most covered</button>'
                 '<button class="fbtn" data-group="sort" data-val="lop">Most lopsided</button>')

    feat_html = "".join(featured_card(s, hero=(i < 2)) for i, s in enumerate(featured))
    more_block = ('<div id="morewrap"><div class="section-label">More Coverage</div>'
                  '<div id="morelist">' + "".join(list_row(s) for s in more) + '</div></div>') if more else '<div id="morelist"></div>'

    head = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>BiasFeed · Politics</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">')

    body = (f'<body><div class="wrap"><header>'
            f'<div class="wordmark">Bias<span>Feed</span></div>'
            f'<div class="tagline">political coverage across the spectrum</div>'
            f'<div class="sub">{len(stories)} political stories · {blind} blindspots · '
            f'{source_count} rated outlets · scope: {scope} · generated {generated_at}</div>'
            f'<div class="legend"><span><i style="background:var(--L)"></i>Left</span>'
            f'<span><i style="background:var(--C)"></i>Center</span>'
            f'<span><i style="background:var(--R)"></i>Right</span></div></header>'
            f'<div class="filterbar">'
            f'<div class="fgroup"><span class="flabel">Lean</span>{lean_btns}</div>'
            f'<div class="fgroup"><span class="flabel">Topic</span>{topic_btns}</div>'
            f'<div class="fgroup"><span class="flabel">Sort</span>{sort_btns}</div>'
            f'</div>'
            f'<div id="featured">{feat_html}</div>'
            f'<div id="featempty">No stories match these filters.</div>'
            f'{more_block}'
            f'<footer><b>These bias ratings are editorial, opinionated calls — not an '
            f'authoritative source.</b> Each outlet is labeled in <code>sources.csv</code> and the '
            f'labels are freely editable. Filtered to political coverage via editable buckets in '
            f'<code>political_topics.txt</code> (scope: <b>{scope}</b>; flip with '
            f'<code>--scope strict</code>). The bias bar shows the share of <em>outlets</em> '
            f'covering a story by lean. A blindspot is a story one side covers heavily while '
            f'the other barely touches it.</footer></div></body></html>')

    return head + CSS + body + JS


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.csv")
    ap.add_argument("--topics", default="political_topics.txt")
    ap.add_argument("--out", default="news.html")
    ap.add_argument("--sim", type=float, default=SIM_THRESHOLD)
    ap.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    ap.add_argument("--scope", choices=["loose", "strict"], default=POLITICAL_SCOPE)
    args = ap.parse_args()

    print("Loading sources & topics...", file=sys.stderr)
    sources = load_sources(args.sources)
    buckets = load_topics(args.topics, args.scope)

    print(f"Fetching {len(sources)} feeds...", file=sys.stderr)
    articles = fetch_articles(sources, args.max_age, PER_FEED_LIMIT)
    print(f"Collected {len(articles)} articles. Clustering...", file=sys.stderr)

    clusters = cluster(articles, args.sim)
    stories = []
    for c in clusters:
        if len({a["source"] for a in c}) < MIN_CLUSTER_SOURCES:
            continue
        topics = story_topics(c, buckets)
        if not topics:
            continue
        s = summarize_story(c)
        s["topics"] = topics
        stories.append(s)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filter_topics = {name for name, tier, _rx in buckets if tier == "loose"}
    out_html = render_html(stories, generated_at, len(sources), args.scope, filter_topics)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_html)
    bs = sum(1 for s in stories if s["blindspot"])
    print(f"\nWrote {len(stories)} political stories ({bs} blindspots) to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
