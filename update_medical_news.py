#!/usr/bin/env python3
"""
MedWeekly 醫學週報自動更新腳本
================================
用途：每週自動爬取台灣醫學新聞 + NEJM/Lancet 文章，更新 index.html，push 至 GitHub Pages

執行方式：
  python update_medical_news.py

需要安裝：
  pip install requests beautifulsoup4 openai

環境變數（選填，使用 OpenAI 做摘要翻譯）：
  OPENAI_API_KEY=sk-...
  GITHUB_TOKEN=ghp_...   （若使用 HTTPS push 需要）
"""

import json
import os
import re
import sys
import time
import datetime
import subprocess
import shutil
import unicodedata
from pathlib import Path
from typing import Optional

# ─── 可設定參數 ─────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent.resolve()
DATA_FILE    = SCRIPT_DIR / "medical_news_data.json"
WEEKLY_FILE  = SCRIPT_DIR / "weekly.html"   # 週報內容所在檔
INDEX_FILE   = SCRIPT_DIR / "index.html"    # 首頁（不含週報資料）
HTML_TEMPLATE = SCRIPT_DIR / "index_template.html"
MAX_WEEKS    = 3          # 保留最近幾週資料
GITHUB_REPO  = "https://github.com/chiufw-max/aievolution.git"
GITHUB_BRANCH = "main"

# NEJM / Lancet 搜尋關鍵詞
ACADEMIC_KEYWORDS = [
    "liver", "hepatic", "hepatology", "gallbladder", "biliary",
    "gastric", "stomach", "colorectal", "colon", "intestine",
    "obesity", "weight loss", "GLP-1", "semaglutide",
    "metabolic syndrome", "MASLD", "MASH", "NASH"
]

TAIWAN_SOURCES = [
    {"name": "Heho健康",  "url": "https://heho.com.tw/",              "rss": "https://heho.com.tw/feed/"},
    {"name": "衛福部",    "url": "https://www.mohw.gov.tw/",          "rss": "https://www.mohw.gov.tw/rss.aspx"},
    {"name": "聯合元氣",  "url": "https://health.udn.com/",           "rss": "https://health.udn.com/rssfeed/articles/0/0/cate"},
    {"name": "Yahoo健康", "url": "https://tw.news.yahoo.com/health/", "rss": "https://tw.news.yahoo.com/rss/health"},
    {"name": "照護線上",  "url": "https://www.careonline.com.tw/",    "rss": "https://www.careonline.com.tw/feed/"},
]

# ─── 台灣新聞主題篩選 ────────────────────────────────────────────
# 邱醫師專注領域：肝膽腸胃、減重、藥物、健康食品
# 每個類別 (權重, 關鍵字列表)；命中越多分數越高，0 分代表過濾掉
TAIWAN_TOPIC_KEYWORDS = {
    "肝膽腸胃": (3, [
        "肝", "膽", "胃", "腸", "胰", "消化", "食道",
        "脂肪肝", "肝炎", "肝硬化", "肝癌", "MASLD", "MASH", "NAFLD", "NASH",
        "胃食道逆流", "GERD", "胃潰瘍", "幽門", "螺旋桿菌",
        "膽結石", "膽囊", "膽道", "胰臟", "胰臟炎",
        "大腸癌", "結腸", "直腸", "克隆", "潰瘍性結腸",
        "便秘", "腹瀉", "痔瘡", "脹氣", "腹脹",
        "胃鏡", "大腸鏡", "內視鏡", "息肉",
    ]),
    "減重代謝": (3, [
        "減重", "減肥", "肥胖", "體重", "BMI", "腰圍",
        "代謝症候群", "代謝", "胰島素", "血糖", "糖尿病",
        "GLP-1", "semaglutide", "瘦瘦針", "Mounjaro", "tirzepatide", "減重藥",
        "司美格魯肽", "替爾泊肽",
    ]),
    "藥物治療": (2, [
        "藥物", "用藥", "新藥", "藥品", "處方", "標靶藥", "免疫療法",
        "健保給付", "FDA核准", "副作用", "藥廠", "原廠藥", "學名藥",
        "抗生素", "胃藥", "瀉藥", "制酸劑", "PPI", "質子幫浦",
    ]),
    "健康食品": (2, [
        "保健食品", "健康食品", "保健品", "補充品", "膳食補充",
        "益生菌", "益菌", "酵素", "膳食纖維", "魚油", "Omega",
        "維生素", "維他命", "葉黃素", "膠原蛋白", "薑黃", "奶薊",
        "營養標示", "食品標示",
    ]),
}

# 排除明顯不相關（即使有關鍵字命中也不要，例：「中風後遺症的腦損傷」會誤中「腸」）
EXCLUDE_KEYWORDS = [
    "失智", "阿茲海默", "巴金森", "視力", "白內障", "青光眼",
    "聽力", "耳鳴", "牙齒", "牙周", "皮膚癌", "黑色素瘤",
]


def score_taiwan_article(title: str, summary: str) -> tuple:
    """計算文章與目標主題的相關度
    Returns: (score, primary_category) — score 0 表示應過濾掉
    """
    text = (title or "") + "  " + (summary or "")
    text_lower = text.lower()

    for ex in EXCLUDE_KEYWORDS:
        if ex in text:
            return (0, "")

    score = 0
    cat_scores = {}
    for cat, (weight, kws) in TAIWAN_TOPIC_KEYWORDS.items():
        cat_score = 0
        for kw in kws:
            if kw.lower() in text_lower:
                cat_score += weight
        if cat_score > 0:
            cat_scores[cat] = cat_score
            score += cat_score

    if score == 0:
        return (0, "")
    primary = max(cat_scores.items(), key=lambda x: x[1])[0]
    return (score, primary)

# ─── 依賴檢查 ────────────────────────────────────────────────────
def check_deps():
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        print("[安裝依賴] pip install requests beautifulsoup4")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "requests", "beautifulsoup4", "--quiet"])
        import requests
        from bs4 import BeautifulSoup
    return requests, BeautifulSoup


# ─── 全文爬取 & 摘要生成 ──────────────────────────────────────────

def fetch_article_body(requests, BeautifulSoup, url: str, source: str) -> str:
    """
    依來源抓取文章全文，回傳純文字（最多 1200 字供摘要使用）。
    各來源選用最適合的 CSS selector 定位主體內容。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除雜訊標籤
        for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                                   "aside", "figure", "figcaption", "noscript",
                                   "iframe", "form", "button"]):
            tag.decompose()

        # 依來源選擇主體 selector
        source_selectors = {
            "Heho健康":  [".entry-content", ".post-content", "article"],
            "衛福部":    [".content-body", ".article-content", ".page-content",
                          ".main-content", "main", ".content"],
            "聯合元氣":  [".story-body", ".article-body", ".article-content", "article"],
            "Yahoo健康": [".caas-body", ".article-body", "[data-component='ArticleBody']"],
            "照護線上":  [".entry-content", ".post-content", "article"],
        }
        candidates = source_selectors.get(source, []) + [
            "article", ".article", "main", ".content", "#content", ".post-body"
        ]

        body_el = None
        for sel in candidates:
            el = soup.select_one(sel)
            if el:
                body_el = el
                break
        if not body_el:
            body_el = soup.find("body")

        # 提取有效段落
        paragraphs = []
        for p in (body_el or soup).find_all(["p", "li", "h2", "h3"], limit=30):
            text = p.get_text(" ", strip=True)
            if len(text) > 25:
                paragraphs.append(text)

        full_text = " ".join(paragraphs)
        full_text = re.sub(r"\s+", " ", full_text).strip()
        return full_text[:1200]
    except Exception:
        return ""


def summarize_article_openai_news(text: str, title: str) -> str:
    """
    用 OpenAI GPT-4o-mini 將文章全文摘要為繁體中文（150–250字）。
    需設定環境變數 OPENAI_API_KEY。
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not text:
        return ""
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        prompt = (
            f"以下是一篇醫學健康新聞，標題：《{title}》\n\n"
            f"內文（節錄）：\n{text[:800]}\n\n"
            "請用繁體中文寫一段 150–250 字的摘要，"
            "涵蓋核心發現、數據與建議，不要出現「摘要：」等前綴，直接呈現摘要內容。"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [!] OpenAI 摘要失敗: {e}")
        return ""


def extract_smart_summary(full_text: str, rss_desc: str, max_chars: int = 350) -> str:
    """
    無 OpenAI 時的 fallback 摘要策略：
    1. 若 RSS 描述夠完整（≥80字且非「請點擊」）→ 直接用
    2. 否則取全文前段，截到最後一個完整句子
    """
    clean_rss = re.sub(r"\s+", " ", (rss_desc or "")).strip()
    if len(clean_rss) >= 80 and "請點擊" not in clean_rss:
        return clean_rss[:max_chars]
    clean_full = re.sub(r"\s+", " ", (full_text or "")).strip()
    if clean_full:
        snippet = clean_full[:max_chars]
        last_period = max(snippet.rfind("。"), snippet.rfind("？"), snippet.rfind("！"))
        if last_period > max_chars // 2:
            snippet = snippet[:last_period + 1]
        return snippet
    return "（請點擊閱讀全文）"


def _scrape_mohw_news(requests, BeautifulSoup, headers: dict) -> list:
    """爬取衛福部新聞稿列表（RSS 失效時的備援）"""
    results = []
    candidate_urls = [
        "https://www.mohw.gov.tw/cp-3506.html",
        "https://www.mohw.gov.tw/mp-1.html",
    ]
    for page_url in candidate_urls:
        try:
            resp = requests.get(page_url, headers=headers, timeout=12)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a_tag in soup.select("a[href]"):
                title = a_tag.get_text(strip=True)
                href  = a_tag.get("href", "")
                if not title or len(title) < 10:
                    continue
                if href.startswith("/"):
                    href = "https://www.mohw.gov.tw" + href
                elif not href.startswith("http"):
                    continue
                if "mohw.gov.tw" not in href:
                    continue
                results.append({
                    "title":   title,
                    "url":     href,
                    "summary": "（請點擊連結閱讀全文）",
                    "source":  "衛福部",
                    "pub":     "",
                })
            if results:
                break
        except Exception:
            continue
    seen, unique = set(), []
    for a in results[:30]:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    return unique


# ─── 台灣新聞爬蟲 ────────────────────────────────────────────────
def fetch_taiwan_news(requests, BeautifulSoup, top_n=5):
    """從 RSS 與網頁爬取台灣醫學健康新聞，並依主題（肝膽腸胃/減重/藥物/健康食品）篩選"""
    articles = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9"
    }

    # 1) 從各 RSS 抓更多文章（25 篇/源）給篩選器更多原料
    for src in TAIWAN_SOURCES:
        if not src.get("rss"):
            continue
        try:
            resp = requests.get(src["rss"], headers=headers, timeout=10)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "xml")
            items = soup.find_all("item")[:25]
            for item in items:
                title = (item.find("title") or {}).get_text("", strip=True)
                link  = (item.find("link") or {}).get_text("", strip=True)
                desc  = (item.find("description") or {}).get_text("", strip=True)
                desc  = re.sub(r"<[^>]+>", "", desc)[:300]
                pub   = (item.find("pubDate") or {}).get_text("", strip=True)
                if title and link:
                    articles.append({
                        "title":   title,
                        "url":     link,
                        "summary": desc or "（請點擊連結閱讀全文）",
                        "source":  src["name"],
                        "pub":     pub,
                    })
        except Exception as e:
            print(f"  [!] {src['name']} RSS 失敗: {e}")

    # 補充：爬取 RSS 失效或無 RSS 的來源（衛福部、Yahoo健康備援）
    existing_titles = {a["title"] for a in articles}
    for src in TAIWAN_SOURCES:
        if src.get("rss"):
            continue  # 已由 RSS 取得，跳過
        try:
            new_items: list = []
            if "mohw.gov.tw" in src["url"]:
                new_items = _scrape_mohw_news(requests, BeautifulSoup, headers)
            elif "yahoo.com" in src["url"]:
                resp = requests.get("https://tw.news.yahoo.com/health/",
                                    headers=headers, timeout=10)
                soup_yh = BeautifulSoup(resp.text, "html.parser")
                for a_tag in soup_yh.select("a[href]"):
                    t = a_tag.get_text(strip=True)
                    h = a_tag.get("href", "")
                    if len(t) > 10 and "yahoo.com" in h and t not in existing_titles:
                        new_items.append({
                            "title":   t,
                            "url":     h,
                            "summary": "（請點擊連結閱讀全文）",
                            "source":  "Yahoo健康",
                            "pub":     "",
                        })
                new_items = new_items[:15]
            for item in new_items:
                if item["title"] not in existing_titles:
                    articles.append(item)
                    existing_titles.add(item["title"])
        except Exception as e:
            print(f"  [!] {src['name']} 補充爬取失敗: {e}")

    # 2) 去重
    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    # 3) 主題篩選 + 評分
    scored = []
    for a in unique:
        score, category = score_taiwan_article(a["title"], a["summary"])
        if score > 0:
            a["_score"] = score
            a["_category"] = category
            scored.append(a)

    # 4) 依分數排序，分數高者優先
    scored.sort(key=lambda x: x["_score"], reverse=True)

    # 5) 若篩選後 < top_n，印出警告（但仍用篩過的內容）
    print(f"  ✓ 從 {len(unique)} 篇候選中篩出 {len(scored)} 篇符合主題")
    if len(scored) < top_n:
        print(f"  [!] 篩選結果不足 {top_n} 篇，本週僅 {len(scored)} 篇符合「肝膽腸胃/減重/藥物/健康食品」")

    selected = scored[:top_n]

    # 6) 抓取各文章全文，並生成完整摘要（取代 RSS 短描述）
    print(f"  抓取 {len(selected)} 篇文章全文並生成摘要…")
    for a in selected:
        try:
            full_text = fetch_article_body(requests, BeautifulSoup, a["url"], a["source"])
            if os.getenv("OPENAI_API_KEY") and full_text:
                ai_summary = summarize_article_openai_news(full_text, a["title"])
                if ai_summary:
                    a["summary"] = ai_summary
                    time.sleep(0.5)
                    continue
            # Fallback：智慧擷取全文前段或 RSS 描述
            a["summary"] = extract_smart_summary(full_text, a.get("summary", ""))
        except Exception as e:
            print(f"  [!] 全文抓取失敗（{a['title'][:20]}…）: {e}")
        time.sleep(0.3)

    # 7) 包成最終格式
    today = datetime.date.today().strftime("%Y%m%d")
    result = []
    for i, a in enumerate(selected):
        result.append({
            "id":      f"tw-{today}-{i+1:02d}",
            "title":   a["title"],
            "summary": a["summary"],
            "source":  a["source"],
            "url":     a["url"],
            "tag":     a["_category"],   # 直接用篩選器判定的主題作為 tag
        })
    return result


def guess_tag(title: str) -> str:
    """依關鍵字猜測新聞標籤"""
    mapping = {
        "癌": "腫瘤學", "腫瘤": "腫瘤學",
        "失智": "神經科", "中風": "神經科", "腦": "神經科",
        "心臟": "心血管", "血壓": "心血管", "心血管": "心血管",
        "糖尿病": "內分泌", "血糖": "內分泌", "甲狀腺": "內分泌",
        "肝": "肝膽腸胃", "膽": "肝膽腸胃", "胃": "肝膽腸胃", "腸": "肝膽腸胃",
        "肥胖": "減重代謝", "減重": "減重代謝", "代謝": "減重代謝",
        "健保": "健保政策", "長照": "長照政策", "政策": "醫療政策",
        "中藥": "中醫藥", "中醫": "中醫藥",
        "疫苗": "感染免疫", "新冠": "感染免疫",
        "營養": "營養醫學", "飲食": "營養醫學",
    }
    for kw, tag in mapping.items():
        if kw in title:
            return tag
    return "醫療"


# ─── PubMed 爬蟲（NEJM + Lancet + JAMA + Gastroenterology + Hepatology）────────
# 每本期刊：(PubMed Journal Name, ISSN, 每週要抓幾篇)
JOURNALS = {
    "nejm":     ("New England Journal of Medicine",     "0028-4793", 4),
    "lancet":   ("The Lancet",                          "0140-6736", 4),
    "jama":     ("JAMA",                                "0098-7484", 4),
    "gastro":   ("Gastroenterology",                    "0016-5085", 4),
    "hepato":   ("Hepatology",                          "0270-9139", 4),
    # 主題型專區（混合多本期刊；fetch_pubmed_articles 已棄用，這裡的期刊名僅作為佔位符）
    "diabetes": ("Diabetes Care",                       "0149-5992", 3),
    "ckd":      ("Kidney International",                "0085-2538", 3),
}

def fetch_pubmed_articles(requests, days=14):
    """
    透過 PubMed E-utilities 搜尋 5 大期刊近期消化/代謝文章
    回傳 {"nejm": [...], "lancet": [...], "jama": [...], "gastro": [...], "hepato": [...]}
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days)).strftime("%Y/%m/%d")
    end   = today.strftime("%Y/%m/%d")

    keyword_query = " OR ".join(f'"{kw}"[tiab]' for kw in ACADEMIC_KEYWORDS[:12])
    results = {key: [] for key in JOURNALS}

    for key, (journal_name, issn, want_n) in JOURNALS.items():
        query = (
            f'({keyword_query}) '
            f'AND ("{journal_name}"[Journal] OR "{issn}"[ISSN]) '
            f'AND ("{start}"[PDAT] : "{end}"[PDAT])'
        )
        try:
            # Search — 多抓 2 篇做緩衝（有些文章可能取不到 abstract）
            r = requests.get(f"{base}/esearch.fcgi", params={
                "db": "pubmed", "term": query, "retmax": want_n + 2,
                "retmode": "json", "sort": "pub+date"
            }, timeout=15)
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                print(f"  [!] {journal_name}: PubMed 無搜尋結果（近{days}天）")
                continue

            # Fetch summaries
            r2 = requests.get(f"{base}/esummary.fcgi", params={
                "db": "pubmed", "id": ",".join(ids),
                "retmode": "json"
            }, timeout=15)
            summaries = r2.json().get("result", {})

            today_str = datetime.date.today().strftime("%Y%m%d")
            for i, pmid in enumerate(ids[:want_n]):
                art = summaries.get(pmid, {})
                title = art.get("title", "").rstrip(".")
                abstract = fetch_abstract(requests, pmid) or "（摘要請見原文）"
                authors = art.get("authors", [])
                author_str = authors[0].get("name","") if authors else ""
                doi = next((l.get("value","") for l in art.get("articleids",[]) if l.get("idtype")=="doi"), "")
                url = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

                # 取 category
                cat = guess_academic_category(title + " " + abstract[:200])

                results[key].append({
                    "id":          f"{key}-{today_str}-{i+1:02d}",
                    "title":       title,
                    "abstract":    abstract[:600],
                    "abstract_zh": f"【請使用翻譯功能】{abstract[:200]}…",
                    "url":         url,
                    "category":    cat,
                    "pmid":        pmid,
                    "author":      author_str,
                })
                time.sleep(0.4)  # PubMed rate limit

        except Exception as e:
            print(f"  [!] {journal_name} PubMed 失敗: {e}")

    return results


def fetch_abstract(requests, pmid: str) -> Optional[str]:
    """從 PubMed 取得全文摘要"""
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        r = requests.get(f"{base}/efetch.fcgi", params={
            "db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "text"
        }, timeout=12)
        text = r.text
        # 擷取 Abstract 部分
        m = re.search(r"(?:ABSTRACT|Abstract)\n(.+?)(?:\n\n|\Z)", text, re.DOTALL)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return re.sub(r"\s+", " ", text[200:800]).strip()
    except:
        return None


def guess_academic_category(text: str) -> str:
    text_lower = text.lower()
    cats = {
        "肝臟": ["liver","hepatic","hepatitis","masld","mash","nash","cirrhosis","hcc","hepatocellular"],
        "膽道": ["gallbladder","biliary","cholecystitis","cholelithiasis","bile"],
        "胃癌": ["gastric cancer","stomach cancer","gastric adenocarcinoma"],
        "腸道": ["colorectal","colon","intestine","ibd","crohn","colitis"],
        "減重": ["obesity","overweight","weight loss","bariatric","bmi"],
        "代謝": ["metabolic","diabetes","insulin","glp-1","semaglutide","tirzepatide"],
    }
    for cat, keywords in cats.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return "消化代謝"


# ─── 翻譯（選填）────────────────────────────────────────────────
def translate_abstracts_openai(articles: list) -> list:
    """若設定 OPENAI_API_KEY，使用 GPT 翻譯摘要"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return articles
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        for a in articles:
            if "【請使用翻譯功能】" in a.get("abstract_zh", ""):
                prompt = (
                    "請將以下英文醫學摘要翻譯為繁體中文，保持專業術語，"
                    "不超過250字：\n\n" + a["abstract"][:500]
                )
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=400
                )
                a["abstract_zh"] = resp.choices[0].message.content.strip()
                time.sleep(0.5)
    except Exception as e:
        print(f"  [!] OpenAI 翻譯失敗: {e}")
    return articles


# ─── 資料管理 ────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"weeks": []}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_week_label() -> str:
    today = datetime.date.today()
    week_num = today.isocalendar()[1]
    return f"{today.year} 第{week_num}週"


def add_week(data: dict, taiwan_news: list, academic: dict) -> dict:
    today = datetime.date.today().strftime("%Y-%m-%d")
    # 若本週已存在則覆蓋
    weeks = [w for w in data["weeks"] if w["date"] != today]
    weeks.insert(0, {
        "date":        today,
        "week_label":  get_week_label(),
        "taiwan_news": taiwan_news,
        "academic":    academic,
    })
    # 保留最近 MAX_WEEKS 週
    data["weeks"] = weeks[:MAX_WEEKS]
    return data


# ─── HTML 生成（預渲染 Pre-rendered Static HTML） ──────────────
def html_escape(s: str) -> str:
    """HTML 跳脫"""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def render_taiwan_cards(items: list) -> str:
    """渲染台灣新聞為靜態 HTML 卡片"""
    link_svg = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 '
                '01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/>'
                '<line x1="10" y1="14" x2="21" y2="3"/></svg>')
    out = []
    for i, n in enumerate(items):
        out.append(
            f'<a class="news-card scroll-reveal" href="{html_escape(n["url"])}" '
            f'target="_blank" rel="noopener" id="{html_escape(n["id"])}">\n'
            f'  <span class="news-number">{i+1:02d}</span>\n'
            f'  <div class="news-body">\n'
            f'    <div class="news-tags">\n'
            f'      <span class="tag tag-tw">{html_escape(n.get("tag","醫療"))}</span>\n'
            f'      <span class="tag tag-source">{html_escape(n["source"])}</span>\n'
            f'    </div>\n'
            f'    <p class="news-title">{html_escape(n["title"])}</p>\n'
            f'    <p class="news-summary">{html_escape(n["summary"])}</p>\n'
            f'  </div>\n'
            f'  <span class="news-link-icon">{link_svg}</span>\n'
            f'</a>'
        )
    return "\n".join(out)


JOURNAL_LABELS = {
    "nejm":     "NEJM",
    "lancet":   "The Lancet",
    "jama":     "JAMA",
    "gastro":   "Gastroenterology",
    "hepato":   "Hepatology",
    # 主題型專區的預設 badge label（render 時若文章自帶 journal 欄位則優先使用文章自己的）
    "diabetes": "糖尿病・代謝",
    "ckd":      "腎臟・CKD",
}

def render_academic_cards(items: list, journal_type: str) -> str:
    """渲染學術文章為靜態 HTML 卡片（journal_type: nejm/lancet/jama/gastro/hepato）"""
    arrow_svg = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
                 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                 'stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/>'
                 '<polyline points="12 5 19 12 12 19"/></svg>')
    default_label = JOURNAL_LABELS.get(journal_type, journal_type.upper())
    out = []
    for a in items:
        # 主題型專區（diabetes/ckd）的文章自帶 journal 欄位，用作 badge label；
        # 純期刊 section（nejm/lancet/jama/gastro/hepato）也可帶 journal，但通常等同 default_label
        badge_label = a.get("journal") or default_label
        out.append(
            f'<div class="academic-card {journal_type} scroll-reveal" '
            f'id="{html_escape(a["id"])}">\n'
            f'  <div class="academic-journal">\n'
            f'    <span class="journal-badge {journal_type}">{html_escape(badge_label)}</span>\n'
            f'    <span class="academic-category">{html_escape(a["category"])}</span>\n'
            f'  </div>\n'
            f'  <p class="academic-title">{html_escape(a["title"])}</p>\n'
            f'  <div>\n'
            f'    <div class="abstract-tabs">\n'
            f'      <button class="tab-btn active" type="button" onclick="switchTab(this,\'en\')">EN</button>\n'
            f'      <button class="tab-btn" type="button" onclick="switchTab(this,\'zh\')">中文</button>\n'
            f'    </div>\n'
            f'    <p class="abstract-text abstract-en">{html_escape(a["abstract"])}</p>\n'
            f'    <p class="abstract-text abstract-zh">{html_escape(a["abstract_zh"])}</p>\n'
            f'  </div>\n'
            f'  <a class="read-more" href="{html_escape(a["url"])}" target="_blank" rel="noopener">'
            f'閱讀全文 {arrow_svg}</a>\n'
            f'</div>'
        )
    return "\n".join(out)


def replace_prerender_block(html: str, marker: str, new_content: str) -> str:
    """替換 <!-- BEGIN PRERENDER:xxx --> ... <!-- END PRERENDER:xxx --> 區塊"""
    pattern = (rf"(<!-- BEGIN PRERENDER:{marker} -->)[\s\S]*?"
               rf"(<!-- END PRERENDER:{marker} -->)")
    replacement = f"\\1\n{new_content}\n        \\2"
    new_html, n = re.subn(pattern, replacement, html)
    if n == 0:
        print(f"  [!] 找不到 PRERENDER:{marker} 標記，跳過")
    return new_html


def regenerate_html(data: dict):
    """
    更新 weekly.html 的兩處：
      1. inline DATA 的 JSON（給 history tabs 用）
      2. 預渲染的卡片 HTML（taiwan / nejm / lancet 三個 PRERENDER 區塊）
    這樣首屏可直接顯示卡片，不必等 JS 執行 (LCP 大幅改善)。
    """
    if not WEEKLY_FILE.exists():
        print(f"  [!] {WEEKLY_FILE} 不存在")
        return

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    current = data["weeks"][0] if data["weeks"] else None

    with open(WEEKLY_FILE, encoding="utf-8") as f:
        html = f.read()

    # 1. 替換 inline DATA
    pattern = r"(const DATA\s*=\s*)(\{[\s\S]*?\})\s*;"
    new_html, n = re.subn(pattern, f"\\g<1>{json_str};", html, count=1)
    if n == 0:
        print("  [!] 找不到 const DATA 佔位符")
    else:
        html = new_html

    # 2. 替換預渲染的卡片 HTML
    if current:
        html = replace_prerender_block(
            html, "taiwan",
            render_taiwan_cards(current["taiwan_news"])
        )
        # 5 個期刊各自有對應的 PRERENDER 區塊
        for journal_key in JOURNALS:
            articles = current["academic"].get(journal_key, [])
            html = replace_prerender_block(
                html, journal_key,
                render_academic_cards(articles, journal_key)
            )

        # 3. 更新 week banner（首屏可見內容，避免閃爍）
        banner_html = (f'<strong>{html_escape(current["week_label"])}</strong> '
                       f'&nbsp;·&nbsp; 更新日期：{current["date"]} '
                       f'&nbsp;·&nbsp; <span>腸胃肝膽・代謝領域・自動整理</span>')
        html = re.sub(
            r'(<div class="week-banner" id="week-banner">)[\s\S]*?(</div>)',
            f'\\g<1>{banner_html}\\g<2>',
            html, count=1
        )

    with open(WEEKLY_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ HTML 預渲染已更新：{WEEKLY_FILE}")

    # 4. 更新 sitemap.xml 的 lastmod
    sitemap = SCRIPT_DIR / "sitemap.xml"
    if sitemap.exists():
        today = datetime.date.today().strftime("%Y-%m-%d")
        with open(sitemap, encoding="utf-8") as f:
            sm = f.read()
        sm = re.sub(r"<lastmod>[\d-]+</lastmod>", f"<lastmod>{today}</lastmod>", sm)
        with open(sitemap, "w", encoding="utf-8") as f:
            f.write(sm)
        print(f"  ✓ sitemap.xml lastmod 已更新")


# ─── GitHub 推送 ─────────────────────────────────────────────────
def push_to_github():
    repo_dir = SCRIPT_DIR

    # 設定 git user（若未設定）
    try:
        name = subprocess.check_output(["git", "config", "user.name"], cwd=repo_dir).decode().strip()
    except:
        name = ""
    if not name:
        subprocess.run(["git", "config", "user.email", "chiufw@gmail.com"], cwd=repo_dir)
        subprocess.run(["git", "config", "user.name", "chiufw-max"], cwd=repo_dir)

    # 初始化 git（若尚未）
    if not (repo_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=repo_dir, check=True)
        subprocess.run(["git", "remote", "add", "origin", GITHUB_REPO], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", "-b", GITHUB_BRANCH], cwd=repo_dir)
    else:
        # 確保 remote 正確
        remotes = subprocess.check_output(["git", "remote", "-v"], cwd=repo_dir).decode()
        if "origin" not in remotes:
            subprocess.run(["git", "remote", "add", "origin", GITHUB_REPO], cwd=repo_dir)

    # 加入檔案（含預渲染後的 weekly.html、sitemap、靜態檔）
    files_to_add = [
        "weekly.html", "index.html", "education.html",
        "medical_news_data.json", "sitemap.xml", "robots.txt", "vercel.json",
    ]
    files_to_add = [f for f in files_to_add if (repo_dir / f).exists()]
    subprocess.run(["git", "add"] + files_to_add, cwd=repo_dir, check=True)

    # Commit
    today = datetime.date.today().strftime("%Y-%m-%d")
    msg = f"chore: weekly update {today}"
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=repo_dir, capture_output=True, text=True
    )
    if "nothing to commit" in result.stdout + result.stderr:
        print("  ✓ 無變更，跳過 push")
        return True

    # Push
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        remote_url = GITHUB_REPO.replace("https://", f"https://{token}@")
        push_cmd = ["git", "push", remote_url, f"HEAD:{GITHUB_BRANCH}", "--force"]
    else:
        push_cmd = ["git", "push", "origin", f"HEAD:{GITHUB_BRANCH}", "--force-with-lease"]

    result = subprocess.run(push_cmd, cwd=repo_dir, capture_output=True, text=True)
    if result.returncode == 0:
        print("  ✓ Push 成功！")
        return True
    else:
        print(f"  [!] Push 失敗:\n{result.stderr}")
        print("  → 請手動執行: git push origin main")
        return False


# ─── 主程式 ─────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  MedWeekly 醫學週報自動更新")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 55)

    requests, BeautifulSoup = check_deps()

    # 1. 台灣新聞
    print("\n[1/4] 爬取台灣醫學新聞…")
    taiwan_news = fetch_taiwan_news(requests, BeautifulSoup, top_n=5)
    print(f"  ✓ 取得 {len(taiwan_news)} 則台灣新聞")

    # 2. 5 大期刊（NEJM / Lancet / JAMA / Gastroenterology / Hepatology）
    print("\n[2/4] 搜尋 5 大期刊文章（PubMed）…")
    academic = fetch_pubmed_articles(requests, days=14)
    summary_parts = [f"{JOURNALS[k][0].split()[0]}: {len(academic[k])} 篇" for k in JOURNALS]
    print("  ✓ " + "  ".join(summary_parts))

    # 3. OpenAI 翻譯（選填）— 對每個期刊都翻
    print("\n[3/4] 翻譯摘要…")
    if os.getenv("OPENAI_API_KEY"):
        for k in JOURNALS:
            academic[k] = translate_abstracts_openai(academic[k])
        print("  ✓ OpenAI 翻譯完成")
    else:
        print("  - 未設定 OPENAI_API_KEY，略過翻譯（中文摘要需手動補充）")

    # 若爬取失敗，保留上週資料示意
    data = load_data()
    if not taiwan_news and data["weeks"]:
        print("  [!] 台灣新聞爬取失敗，保留上週資料")
        taiwan_news = data["weeks"][0]["taiwan_news"]
    # 每本期刊獨立 fallback；舊資料可能沒有新期刊的 key，要用 .get() 安全讀取
    if data["weeks"]:
        last_academic = data["weeks"][0].get("academic", {})
        for k in JOURNALS:
            if not academic[k] and last_academic.get(k):
                academic[k] = last_academic[k]

    # 4. 更新資料 & HTML
    print("\n[4/4] 更新資料與 HTML…")
    data = add_week(data, taiwan_news, academic)
    save_data(data)
    regenerate_html(data)

    # 5. Push GitHub
    print("\n[5/5] Push 至 GitHub Pages…")
    push_to_github()

    print("\n✅ 完成！網頁已更新。")


if __name__ == "__main__":
    main()
