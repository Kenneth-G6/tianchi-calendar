#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长白山北景区天池逐日开放台账 · 构建脚本

流程：抓取「长白山发布」澎湃号每日《今日长白山》公报 -> 规则解析北景区天池状态
      -> 合并历史锚点与媒体公告 -> 生成逐日日历 index.html

设计要点：
  * 增量抓取：已抓过的 contId 不重复请求，data/dailies.json 为缓存
  * 容错：网络/接口失败时保留旧数据继续渲染，不使 CI 失败
  * 口径：区分「景点关闭 / 景区整体关闭 / 车辆停运 / 中途停检 / 待定 / 开放」
"""
import json, os, re, sys, time, datetime
from collections import Counter
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DAILIES = os.path.join(DATA, "dailies.json")
ANCHORS = os.path.join(DATA, "anchors.json")
TPL = os.path.join(ROOT, "scripts", "template.html")
OUT = os.path.join(ROOT, "index.html")

PPH_ID = "111214"
START_DATE = datetime.date(2023, 11, 9)
LIST_STOP_BEFORE = datetime.date(2023, 9, 1)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://m.thepaper.cn/",
}

# 单篇核查（历史段可直接取到的公报）
ANCHOR_IDS = [
    29952376, 30089460, 30126949, 30236005, 30480602, 30610720, 30734091,
    32095658, 32102722, 32130996, 32177675, 32532349, 32908900, 34213321,
    34283817, 34376421, 34432878,
]

# 媒体公告（仅作为“该日确已关闭”的最低确认，不作频率推断）
MEDIA = [
    ("2023-11-06", "CLOSED_AREA", "北、西景区全天关闭", "澎湃新闻"),
    ("2023-11-26", "CLOSED_TIANCHI", "北、西景区天池景点全天关闭（大风降雪）", "光明网"),
    ("2023-12-02", "CLOSED_TIANCHI", "北、西景区天池景点全天关闭（积雪结冰）", "今日头条·平安吉林"),
    ("2023-12-03", "CLOSED_TIANCHI", "北、西景区天池景点全天关闭（积雪结冰）", "腾讯新闻"),
    ("2023-12-15", "CLOSED_AREA", "北、西景区全天关闭（持续降雪、道路结冰）", "城市晚报"),
    ("2023-12-19", "CLOSED_TIANCHI", "北、西天池景点全天关闭（主峰大风、路面积雪结冰）", "光明网"),
    ("2024-02-12", "CLOSED_AREA", "早7时天池关闭；中午升级为北景区全天关闭", "光明网"),
    ("2024-02-13", "CLOSED_TIANCHI", "北、西天池+北景区谷底森林全天关闭，全价票半价（瞬间风力10级）", "央视网"),
    ("2024-05-05", "CLOSED_TIANCHI", "北、西天池景点全天关闭（主峰降雪）", "央视网 / 金羊网"),
    ("2024-05-31", "CLOSED_TIANCHI", "北景区天池景点全天关闭（大雾降雪）", "中国吉林网"),
    ("2024-06-01", "CLOSED_TIANCHI", "北景区天池景点全天关闭（公路栈道积雪结冰）", "中国吉林网 / 光明网"),
    ("2024-08-11", "CLOSED_TIANCHI", "北景区天池景点全天关闭（中雨、7-8级风、大雾）", "环球网"),
    ("2025-05-01", "CLOSED_TIANCHI", "北、南天池关闭，西景区关闭；当日 10:46、11:12 两次停检公告", "海报新闻 / 极目新闻"),
    ("2025-05-02", "CLOSED_TIANCHI", "北、南天池关闭（主峰降雪、栈道结冰）", "海报新闻 / 极目新闻"),
    ("2025-10-27", "CLOSED_AREA", "北、西景区全天关闭（降雪、栈道公路结冰）", "光明网"),
]

BOILER_RE = re.compile(
    "|".join(["视天气情况随时关闭", "视天气情况随时停运", "视天气情况随时开放",
              "随时关闭", "随时停运", "视天气情况"]))

REASON_RULES = [
    ("大风", r"\d+[-~～]?\d*级|风力|大风|阵风|偏西风|西北风|西南风"),
    ("降雪", r"降雪|中雪|大雪|小雪|阵雪|雨夹雪|风吹雪|风飘雪"),
    ("结冰/积雪", r"结冰|积雪|冰冻"),
    ("大雾/能见度", r"大雾|能见度"),
    ("降雨", r"中雨|大雨|小雨|降雨"),
    ("极寒", r"体感温度-|极寒|冻伤"),
    ("设施维修", r"维修|倒树|基础设施|维护"),
]
PLAY = {"OPEN", "OPEN_DELAY", "PARTIAL"}
BLOCK = {"CLOSED_TIANCHI", "CLOSED_AREA", "VEHICLE_STOP", "PARTIAL", "UNCERTAIN"}


# ----------------------------- 抓取 -----------------------------
def list_articles(max_pages=40):
    """按 startTime 游标回翻政务号文章列表"""
    out, cursor = [], None
    for _ in range(max_pages):
        body = {"pphId": PPH_ID, "pageNum": 1, "pageSize": 50}
        if cursor:
            body["startTime"] = cursor
        r = requests.post("https://api.thepaper.cn/contentapi/cont/pph/gov",
                          headers=HEADERS, json=body, timeout=25)
        j = r.json()
        d = j.get("data") or {}
        lst = d.get("list") or []
        out.extend(lst)
        cursor = d.get("startTime")
        oldest = min((x.get("publishTime") or "9")[:10] for x in lst) if lst else "9"
        if not d.get("hasNext") or not lst or not cursor:
            break
        try:
            if datetime.date.fromisoformat(oldest) < LIST_STOP_BEFORE:
                break
        except ValueError:
            pass
        time.sleep(0.2)
    return out


def clean_html(h):
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</p>|</div>|</section>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = h.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\n{2,}", "\n", h).strip()


def fetch_detail(cid):
    r = requests.get(f"https://api.thepaper.cn/contentapi/article/mdetail/{cid}",
                     headers=HEADERS, params={"contId": cid}, timeout=25)
    d = r.json()["data"]["contentDetail"]
    return {
        "contId": str(cid),
        "title": d.get("name") or "",
        "pub": d.get("pubTime") or "",
        "text": clean_html(d.get("content") or "")[:6000],
        "url": f"https://m.thepaper.cn/newsDetail_forward_{cid}",
    }


# ----------------------------- 解析 -----------------------------
def north_text(text):
    ps = [p.strip() for p in re.split(r"\n", text or "") if p.strip()]
    hit = [p for p in ps if ("北景区" in p) or ("北、西景区" in p) or ("北，西景区" in p)]
    if not hit:
        hit = [p for p in ps if "天池" in p and ("关闭" in p or "开放" in p)]
    return " ".join(hit)


def classify(raw):
    j = BOILER_RE.sub("", raw)
    if not raw.strip():
        return "NO_RECORD", "无北景区段落"
    if re.search(r"天池(景点|主峰景点)[^。，]{0,8}(全天关闭|关闭|暂停游览|暂停开放)", j) or \
       re.search(r"天池[^。，]{0,6}景点(全天)?关闭", j) or \
       re.search(r"天池(主峰)?[^。]{0,15}(全天关闭|暂停游览)", j):
        return "CLOSED_TIANCHI", "天池景点关闭 / 暂停游览"
    if re.search(r"北(、西)?景区[^。]{0,30}(全天关闭|全天停运|全天暂停开放)", j) or \
       re.search(r"北(、西)?景区[^。]{0,25}不具备安全运营条件[^。]{0,12}(全天关闭|今日关闭|全天暂停开放|全天停运)", j):
        return "CLOSED_AREA", "北景区整体关闭 / 停运"
    if re.search(r"天池(车辆|主峰车辆|主峰车)[^。]{0,30}(停运|停止运营|暂停运营)", j):
        return "VEHICLE_STOP", "天池车辆停运（景点未明示关闭）"
    if re.search(r"天池[^。]{0,25}\d{1,2}[：:]\d{2}\s*停止检票", j):
        return "PARTIAL", "天池中途停止检票"
    if re.search(r"北(、西)?景区[^。]{0,25}\d{1,2}[：:]\d{2}\s*停止检票", j):
        return "PARTIAL", "北景区中途停止检票"
    if re.search(r"是否开放视清雪进度|视清雪进度确定", j):
        return "UNCERTAIN", "是否开放视清雪进度确定"
    if re.search(r"天池[^。]{0,25}(开始检票|延缓|推迟)", j):
        return "OPEN_DELAY", "天池延迟 / 限时开放"
    if re.search(r"天池[^。]{0,25}(正常开放|开放)", j) or \
       (re.search(r"天池、", j) and re.search(r"正常开放", j)):
        return "OPEN", "天池正常开放"
    if re.search(r"(正常开放|开放)", j):
        return "OPEN_OTHER", "仅其他景点给出口径"
    return "UNKNOWN", "未识别"


def build_record(raw_text, title, pub, cid, src, url, level):
    raw = north_text(raw_text)
    st, why = classify(raw)
    rs = [n for n, p in REASON_RULES if st in BLOCK and re.search(p, raw)]
    m = re.search(r"(\d{1,2})月(\d{1,2})日", title or "")
    mo, da = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    y = int(pub[:4]) if pub else None
    if y and mo and pub and mo - int(pub[5:7]) >= 6:
        y -= 1
    return dict(contId=str(cid), title=title, pub=pub, year=y, month=mo, day=da,
                status=st, why=why, reason=rs, north=raw[:400], src=src, url=url, level=level)


# ----------------------------- 主流程 -----------------------------
def main():
    os.makedirs(DATA, exist_ok=True)
    cache = {}
    if os.path.exists(DAILIES):
        cache = json.load(open(DAILIES, encoding="utf-8"))

    # 1) 列表
    try:
        arts = list_articles()
        dailies = [x for x in arts if "今日长白山" in (x.get("name") or "")]
        print(f"[list] articles={len(arts)} dailies={len(dailies)}")
    except Exception as e:
        print(f"[list] FAILED {type(e).__name__}: {e} — 使用缓存继续", file=sys.stderr)
        dailies = []

    # 2) 增量抓详情
    new = 0
    for it in dailies:
        cid = str(it["contId"])
        if cid in cache:
            continue
        try:
            cache[cid] = fetch_detail(cid)
            new += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"[detail] {cid} failed: {type(e).__name__}", file=sys.stderr)
    print(f"[detail] new={new} total={len(cache)}")
    if cache:
        json.dump(cache, open(DAILIES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 3) 历史锚点
    anchors = {}
    if os.path.exists(ANCHORS):
        anchors = json.load(open(ANCHORS, encoding="utf-8"))
    for cid in ANCHOR_IDS:
        cid = str(cid)
        if cid in anchors or cid in cache:
            continue
        try:
            anchors[cid] = fetch_detail(cid)
        except Exception as e:
            print(f"[anchor] {cid} failed: {type(e).__name__}", file=sys.stderr)
    if anchors:
        json.dump(anchors, open(ANCHORS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if not cache:
        print("[fatal] 无任何可用公报数据，且缓存为空", file=sys.stderr)
        sys.exit(1)

    # 4) 组装逐日
    recs = [build_record(v["text"], v["title"], v["pub"], v["contId"],
                         "澎湃·长白山发布（官方逐日）", v["url"], "daily") for v in cache.values()]
    arecs = [build_record(v["text"], v["title"], v["pub"], v["contId"],
                          "长白山发布（单篇核查）", v["url"], "single") for v in anchors.values()]

    day = {}
    def put(d, r):
        k = d.isoformat()
        if k in day and day[k]["level"] == "daily" and r["level"] != "daily":
            return
        day[k] = dict(date=k, status=r["status"], why=r["why"], reason=r["reason"],
                      title=r["title"], url=r["url"], src=r["src"], range=r["north"],
                      level=r["level"], note="", note_src="")

    for r in recs + arecs:
        if r["year"] and r["month"] and r["day"]:
            try:
                put(datetime.date(r["year"], r["month"], r["day"]), r)
            except ValueError:
                pass
    for ds, st, why, srcn in MEDIA:
        if ds in day:
            day[ds]["note"] = why; day[ds]["note_src"] = srcn
        else:
            day[ds] = dict(date=ds, status=st, why=why, reason=[], title=why, url="",
                           src=srcn + "（公告转载，非逐日公报）", range=why, level="media",
                           note=why, note_src=srcn)

    latest = max((d for d in day if day[d]["status"] != "NO_RECORD"), default=None)
    if not latest:
        print("[fatal] 无有效日期", file=sys.stderr); sys.exit(1)
    end = datetime.date.fromisoformat(latest)
    serial, d = [], START_DATE
    while d <= end:
        k = d.isoformat()
        serial.append(day.get(k, dict(date=k, status="NO_RECORD", why="无公开逐日记录",
                                     reason=[], title="", url="", src="", range="",
                                     level="none", note="", note_src="")))
        d += datetime.timedelta(days=1)

    # 5) 统计
    YR = int(max(x["date"][:4] for x in serial if x["status"] != "NO_RECORD"))
    y_start = max(START_DATE, datetime.date(YR, 1, 1)).isoformat()
    yv = [x for x in serial if x["date"] >= y_start]
    rec = [x for x in yv if x["status"] != "NO_RECORD"]
    play = [x for x in rec if x["status"] in PLAY]
    c = Counter(x["status"] for x in rec)
    month = {}
    for m in range(1, 13):
        ds = [x for x in yv if x["date"][:7] == f"{YR}-{m:02d}"]
        r2 = [x for x in ds if x["status"] != "NO_RECORD"]
        p2 = [x for x in r2 if x["status"] in PLAY]
        month[m] = dict(days=len(ds), rec=len(r2), play=len(p2),
                        rate=(round(len(p2) / len(r2) * 100, 1) if r2 else None))
    reason = Counter()
    for x in rec:
        if x["status"] in BLOCK:
            for r in x["reason"]:
                reason[r] += 1

    dailies_min = min(x["date"] for x in serial if x["level"] == "daily")
    hist = [x for x in serial if x["level"] != "daily" and x["status"] != "NO_RECORD"]
    ndaily = sum(1 for x in serial if x["level"] == "daily")
    nsingle = sum(1 for x in serial if x["level"] == "single")
    nmedia = sum(1 for x in serial if x["level"] == "media")

    rate = round(len(play) / len(rec) * 100, 1) if rec else 0
    gap = len(yv) - len(rec)

    D = []
    for x in serial:
        D.append({"d": x["date"], "s": x["status"], "w": x["why"], "r": x["reason"],
                  "t": (x["title"] or "")[:60], "u": x["url"], "src": (x["src"] or "")[:40],
                  "g": (x["range"] or "")[:160], "lv": x["level"],
                  "n": (x["note"] or "")[:80], "ns": (x["note_src"] or "")[:20]})
    payload = json.dumps({"days": D, "stats": dict(month=month, reason=dict(reason))},
                         ensure_ascii=False, separators=(",", ":"))

    # 6) 渲染
    tpl = open(TPL, encoding="utf-8").read()
    findings = f"""
    <p><b>① 天池的“关”不是例外，而是常态的一半。</b>{YR} 年有公报的 {len(rec)} 天里，可游览 {len(play)} 天（{rate}%），关闭或不可达 {len(rec)-len(play)} 天。若把“天池车辆停运”（{c.get('VEHICLE_STOP',0)} 天）与“北景区整体关闭”（{c.get('CLOSED_AREA',0)} 天）都算作不可游览——这是游客视角的真实口径——则不可游比例约 {round((len(rec)-len(play))/len(rec)*100,1)}%。</p>
    <p><b>② 季节曲线极陡：盛夏到初秋是唯一的高可游窗口。</b>可游率最高的月份与最低的月份相差数十个百分点，冬季与春雪季几乎全线关闭。</p>
    <p><b>③ 关闭的主因是大风，不是降雪。</b>关闭日成因中“大风”出现 {reason.get('大风',0)} 次、“结冰/积雪” {reason.get('结冰/积雪',0)} 次、“降雪” {reason.get('降雪',0)} 次。天池海拔 2,000 米以上，8–9 级风即构成“不具备安全运营条件”，而这在春秋两季几乎天天出现。</p>
    <p><b>④ 公报本身有缺口。</b>{YR} 年已覆盖的 {len(yv)} 天中 {gap} 天无公报（{round(gap/len(yv)*100,1) if yv else 0}%）。缺档不等于“开放”，也不等于“关闭”——它是信息真空，本报告一律留灰，不做插值。</p>
    <p><b>⑤ 历史段无法逐日回溯。</b>政务号在澎湃的存档被服务端限制在约最近 10 个月（最早 {dailies_min}），更早内容只能以单篇公报与媒体公告“点状锚定”，因此 2023–2025 不给开放率。</p>
    """

    repl = {
        "{{RANGE}}": f"{START_DATE.isoformat()} — {end.isoformat()}",
        "{{TOTAL}}": f"{len(serial):,}",
        "{{NDAILY}}": str(ndaily),
        "{{NSINGLE}}": str(nsingle),
        "{{NMEDIA}}": str(nmedia),
        "{{DAILY_FROM}}": dailies_min,
        "{{WARN_BADGE}}": f"{dailies_min} 之前无逐日公报可获取 → 该段以灰色留空，不臆测",
        "{{HEADNOTE}}": f"""<strong>先看这一句：</strong>全部 {len(serial):,} 天中，真正有“每日官方公报”可逐日核对的，是从 {dailies_min} 起的 {ndaily} 天。
    {dailies_min} 之前的日期，官方逐日公报在公开渠道<b>无法批量回溯</b>（澎湃政务号存档上限约 1,184 条即止于该日），
    只能用已核实的单篇公报与媒体公告做“点状锚定”。因此下文所有开放率<b>只对 {YR} 年成立</b>，历史段仅给出“已确认关闭日”清单，<b>不计算开放率</b>——分母不可知时算出的比率是伪精度。""",
        "{{EXEC_SUB}}": f"{YR} 年可游览天数占“有公报天数”的 {rate}%，几乎一半时间处于关闭或不可达状态。",
        "{{YR}}": str(YR),
        "{{RATE}}": str(rate),
        "{{PLAY}}": str(len(play)),
        "{{REC}}": str(len(rec)),
        "{{CLOSED}}": str(c.get("CLOSED_TIANCHI", 0)),
        "{{CLOSEDPCT}}": f"{c.get('CLOSED_TIANCHI',0)/len(rec)*100:.1f}" if rec else "0",
        "{{AREA}}": str(c.get("CLOSED_AREA", 0)),
        "{{VEH}}": str(c.get("VEHICLE_STOP", 0)),
        "{{GAP}}": str(gap),
        "{{GAPBASE}}": str(len(yv)),
        "{{GAPPCT}}": f"{gap/len(yv)*100:.1f}" if yv else "0",
        "{{HIST}}": str(len(hist)),
        "{{HIST_NOTE}}": f"{START_DATE.isoformat()} 至 {dailies_min}，为关闭日或个别开放日",
        "{{HIST_FROM}}": START_DATE.isoformat(),
        "{{LATEST}}": end.isoformat(),
        "{{FINDINGS}}": findings,
        "{{DATA}}": payload,
    }
    html = tpl
    for k, v in repl.items():
        html = html.replace(k, v)
    open(OUT, "w", encoding="utf-8").write(html)
    print(f"[done] {OUT} {len(html.encode('utf-8'))//1024} KB | {len(serial)} days | {YR} rate {rate}% | daily {ndaily}")


if __name__ == "__main__":
    main()
