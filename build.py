"""Build the Amsterdam scene map from the Notion base.

Reads three Notion databases (Entities, Tools, Bubbles) with the official API,
turns them into map data and writes site/index.html from template.html.

Env:
  NOTION_TOKEN  Notion integration secret (GitHub secret, never in the page)
  REPO          owner/repo, used for the "Update the map" link (set by the workflow)
  MOCK_DIR      optional: read raw JSON from this folder instead of Notion (testing)
"""
import json, os, sys, time, urllib.request, urllib.error, datetime, pathlib

ENTITIES = "487a73f2c16d46f98b84f2b1ff874ea8"
TOOLS = "64837552a505441e9330c0f038f41098"
BUBBLES = "416ff8fbd08c80328bc4f4ef2538cead"
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
HERE = pathlib.Path(__file__).parent

TOOL_CATS = ["Ticketing", "Resale", "Merch / shop", "Releases", "Mixes / radio",
             "Newsletter / community", "Forms", "Social / link hub", "Website"]


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["NOTION_TOKEN"],
                 "Notion-Version": VERSION, "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt); continue
            msg = e.read().decode(errors="replace")
            sys.exit(f"Notion API error {e.code} on {path}: {msg}\n"
                     "Check that the integration has access to the Ams_Scene_v2 page.")


def query_all(db):
    if os.environ.get("MOCK_DIR"):
        return json.load(open(pathlib.Path(os.environ["MOCK_DIR"]) / f"{db}.json"))
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        r = call("POST", f"/databases/{db}/query", body)
        rows += r["results"]
        if not r.get("has_more"): break
        cursor = r["next_cursor"]
    # relations longer than 25 come back truncated: fetch the full list
    for page in rows:
        for name, p in page["properties"].items():
            if p["type"] == "relation" and p.get("has_more"):
                items, cur = [], None
                while True:
                    q = f"/pages/{page['id']}/properties/{p['id']}" + (f"?start_cursor={cur}" if cur else "")
                    r = call("GET", q)
                    items += [x["relation"] for x in r["results"]]
                    if not r.get("has_more"): break
                    cur = r["next_cursor"]
                p["relation"] = items
    return rows


# ---------- property readers ----------
def nid(x): return x.replace("-", "")
def text(p):
    if not p: return ""
    t = p["type"]
    if t in ("title", "rich_text"): return "".join(x["plain_text"] for x in p[t]).strip()
    if t == "url": return p["url"] or ""
    if t == "select": return (p["select"] or {}).get("name") or ""
    return ""
def multi(p): return [x["name"] for x in (p or {}).get("multi_select", [])]
def rel(p): return [nid(x["id"]) for x in (p or {}).get("relation", [])]


def main():
    ents, tools, bubs = query_all(ENTITIES), query_all(TOOLS), query_all(BUBBLES)

    bubble_name = {nid(b["id"]): text(b["properties"].get("Name")) for b in bubs}
    tool = {}
    for t in tools:
        pr = t["properties"]
        cat = text(pr.get("Category")) or "Newsletter / community"
        tool[nid(t["id"])] = {"name": text(pr.get("Name")), "cat": cat}

    rows = {}
    for e in ents:
        pr = e["properties"]
        rows[nid(e["id"])] = {
            "id": nid(e["id"]), "name": text(pr.get("Name")), "type": text(pr.get("Type")),
            "status": text(pr.get("Status")) or None, "also": multi(pr.get("Also Does")),
            "rel": text(pr.get("Relationship")) or None, "link": text(pr.get("Link")),
            "about": text(pr.get("About")), "notes": text(pr.get("Notes")),
            "notion": e["url"],
            "plays": rel(pr.get("Plays at")), "works": rel(pr.get("Works with")),
            "signed": rel(pr.get("Signed to")), "part": rel(pr.get("Part of")),
            "tools": [tool[t] for t in rel(pr.get("Tools")) if t in tool],
            "bubbles": [bubble_name[b] for b in rel(pr.get("Bubbles")) if b in bubble_name],
        }

    # who is signed to a label/agency (it is shown only if it has a roster in the base)
    has_roster = {t for r in rows.values() for t in r["signed"]}
    keep = {}
    for r in rows.values():
        if not r["name"] or not r["type"]: continue
        if r["type"] == "Label / agency":
            if r["id"] not in has_roster: continue   # artist-run labels become a badge on the artist
            r["type"] = "Agency"
        if r["type"] == "Key agent" and not any(p in rows for p in r["part"]): continue
        keep[r["id"]] = r

    links, seen = [], set()
    def add(a, b, k):
        if a not in keep or b not in keep or a == b: return
        key = (k,) + (tuple(sorted((a, b))) if k == "works" else (a, b))
        if key in seen: return
        seen.add(key); links.append({"s": a, "t": b, "k": k})
    member_ok = {"Collective", "Festival", "Venue"}
    for r in keep.values():
        for t in r["plays"]: add(r["id"], t, "plays")
        for t in r["works"]: add(r["id"], t, "works")
        for t in r["signed"]:
            if keep.get(t, {}).get("type") == "Agency": add(r["id"], t, "signed")
        for t in r["part"]:
            if r["type"] == "Key agent": add(r["id"], t, "agent")
            elif r["type"] == "Artist" and keep.get(t, {}).get("type") in member_ok: add(r["id"], t, "plays")

    nodes = [{k: r[k] for k in ("id", "name", "type", "status", "also", "rel", "link", "about",
                                "notes", "notion", "tools", "bubbles")} for r in keep.values()]
    nodes.sort(key=lambda n: n["name"].lower())
    used = {b for n in nodes for b in n["bubbles"]}
    bubbles = [bubble_name[nid(b["id"])] for b in bubs if bubble_name[nid(b["id"])] in used]

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))
    repo = os.environ.get("REPO", "")
    data = {"snapshot": now.strftime("%-d %b %Y, %H:%M"), "bubbles": bubbles, "nodes": nodes,
            "links": links, "toolCats": TOOL_CATS,
            "updateUrl": f"https://github.com/{repo}/actions/workflows/build.yml" if repo else ""}

    html = (HERE / "template.html").read_text().replace("/*DATA*/null", json.dumps(data, ensure_ascii=False))
    out = HERE / "site"; out.mkdir(exist_ok=True)
    (out / "index.html").write_text(html)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"Built: {len(nodes)} nodes, {len(links)} links, bubbles {bubbles}")


if __name__ == "__main__":
    main()
