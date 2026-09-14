#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
りんごの奴隷（管理画面）のApple Store在庫ページを自動取得するスクリプト。

分かったこと：
- このページはLivewire（Laravel製の仕組み）で動いてて、モデルを選ぶたびに
  「/livewire/update」という通信が飛ぶ。
- ただしその中身は綺麗なJSONじゃなくて、サーバーが作った「HTMLの塊」がまるごと
  文字列として返ってくる形式（components[].effects.html の中）。
- このHTMLを直接POSTリクエストで再現するのは、Livewireの内部トークン
  （snapshot / fingerprint / CSRF）を正しく組み立てる必要があってかなり面倒。
- なので今回は「本物のブラウザを自動操作して、画面に表示された結果をそのまま読む」
  方式（Playwright）にした。買取1丁目の時と同じ考え方。

使い方（初回のみ）：
    pip install playwright beautifulsoup4
    playwright install chromium
    python scrape_ringonodorei_apple.py --login

    → ブラウザが開くので、いつも通りりんごの奴隷にログインする。
      ログインできたらターミナルに戻ってEnterを押す（スクリプトの指示に従う）。
      これで auth_state.json というファイルにログイン状態が保存される。

2回目以降（通常の実行）：
    python scrape_ringonodorei_apple.py

    → auth_state.json を使って自動でログイン状態を再現し、
      モデルを選んで在庫を取得する。

出力：
    ringonodorei_apple_stock.json

注意：
- MODEL_LIST は今回のスクショで見えていた分だけ入れてある。もっと網羅したい場合は
  ドロップダウンの候補をそのまま流用するよう改造できる（要相談）。
- ログイン状態（auth_state.json）は数週間〜数ヶ月で切れることがある。
  「ログインが切れてるかも」というエラーが出たら、もう一度 --login で
  やり直してほしい。
- このサンドボックス環境からは対象サイトにアクセスできないので未実行・未検証。

【2026/09/13 追記】
1回目の実行は全モデルで「Locator.click: Timeout 30000ms exceeded」になった。
原因は、検索欄を「表示されてる文字（プレースホルダー）」で掴もうとしてたけど、
Playwrightのget_by_textはプレースホルダーを拾ってくれないので、
そもそも検索欄自体をクリックできてなかった。
→ get_by_placeholder に変更し、クリック後に検索文字を入力してから候補をクリックする
  流れに直した。
→ それでも同じ場所で失敗する場合は、.fill() で一気に値を入れてもLivewire側の
  絞り込みJSが反応しないケースを疑い、キーボードで1文字ずつ打つ方式（page.keyboard.type）
  に変更。失敗時は debug_<モデル名>.png にその瞬間の画面を保存するようにした。
→ debug画像を見たところ、画面上部の「iPhone 17」「iPhone 18」タブが常に
  「iPhone 18」側のままになっていて、iPhone 17系モデルの候補がそもそも
  存在せずタイムアウトしていたと判明。機種名に "18" が含まれるかどうかで、
  先に正しいタブをクリックしてから検索するように修正した。
→ タブの修正後もget_by_placeholderが検索欄を見つけられず失敗。プレースホルダー
  文字列の完全一致に頼らず、CSSの属性セレクター（input[placeholder*="モデル"]等）
  を複数パターン順番に試す方式に変更した。
→ それでも「検索ボックスが見つからなかった」で失敗。おそらく「モデルを検索または
  選択...」は閉じた状態の見た目だけで、クリックした瞬間に本物のinputがDOMに
  現れる作り。なので先にその見た目部分をクリックしてドロップダウンを開いてから、
  改めてinputを探す2段階の流れに変更した。
→ この修正でクリック自体は成功するようになったが、今度は「0店舗分取得」に。
  検索・選択・在庫取得の通信までは通ってるが、HTMLから店舗名と在庫状況を
  読み取る正規表現が実際の並びと合っていない。改行の有無を仮定しすぎていたので
  緩めのパターンに直し、さらに0件だった時は読み取った本文を
  debug_text_<モデル名>.txt に保存するようにした（次に失敗したらこれを見て
  正規表現を実物に合わせて調整する）。
→ 11店舗分取得できるようになったが、店舗名が「Apple 渋」のように途中で
  切れてしまっていた。店舗名部分の正規表現が非貪欲（最短一致）で1文字だけ
  拾ってしまっていたのが原因。日本語の文字クラスに限定した貪欲マッチに
  直して、店舗名がフルで取れるようにした。

【GitHub Actionsでの自動運用について】
- update-apple-stock.yml というワークフローを別途用意した。これをリポジトリの
  .github/workflows/ フォルダに置くと、毎日決まった時間に自動でこのスクリプトが
  実行され、結果がリポジトリに自動コミットされる。
- ログイン状態（auth_state.json の中身）を、GitHubリポジトリの
  Settings > Secrets and variables > Actions で
  「RINGONODOREI_AUTH_STATE」という名前のSecretとして保存しておく必要がある。
- ログインセッションは時間が経つといずれ切れる。切れた場合、このスクリプトは
  「全モデル0件」を検知してファイルを上書きせず終了するので、既存の正しい
  データが消えることはない（ただし更新も止まるので、たまにログインし直して
  Secretを更新する必要がある）。
"""

import argparse
import json
import re
import time
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://ringonodorei.com/admin/apple-store-inventory"
AUTH_FILE = "auth_state.json"

# 調べたいモデル（画面のドロップダウンに出てくる表記と完全に一致させる）
MODEL_LIST = [
    "iPhone 17 Pro Max 256GB シルバー",
    "iPhone 17 Pro Max 256GB ディープブルー",
    "iPhone 17 Pro Max 256GB コズミックオレンジ",
    "iPhone 17 Pro 256GB シルバー",
    "iPhone 17 Pro 256GB ディープブルー",
    "iPhone 17 256GB ブラック",
    "iPhone 18 Pro Max 256GB ブラック",
    # 必要な分だけ増やしてください（ドロップダウン候補と同じ表記で）
]


def do_login():
    """初回：手動でログインしてもらい、その状態を保存する"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE_URL)
        print("\nブラウザでいつも通りログインしてください。")
        input("ログインできたら、ここに戻ってEnterキーを押してください...")
        context.storage_state(path=AUTH_FILE)
        browser.close()
        print(f"ログイン状態を {AUTH_FILE} に保存したよ。次回からは python scrape_ringonodorei_apple.py だけでOK。")


def parse_store_html(html):
    """店舗カードのHTMLから「店舗名: 在庫あり/なし」を抜き出す"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    result = {}
    # 店舗名と在庫状況が同じ行にあるか、間に改行が挟まるかが分からないため、
    # 改行をまたいでもOKな緩めのパターンにしてある（[\s\S]で改行も含めて許容）
    for m in re.finditer(r"(Apple\s*[ぁ-んァ-ヶ一-龠ー]{1,8})[\s\S]{0,20}?(在庫あり|在庫なし)", text):
        store, status = m.groups()
        result[store.strip()] = (status == "在庫あり")
    return result, text


def fetch_model_stock(page, model_label):
    # 画面上部の「iPhone 17」「iPhone 18」タブを、機種名に応じて先に選ぶ。
    # これをしないと、違うタブが選ばれたままだと候補にそのモデルが出てこない。
    if "18" in model_label:
        page.get_by_text("iPhone 18", exact=True).click()
    else:
        page.get_by_text("iPhone 17", exact=True).click()
    page.wait_for_timeout(500)

    # 「モデルを検索または選択...」という表示は、クリックする前は見た目だけの
    # 閉じた状態で、実際のinput要素はクリックした瞬間にDOMへ現れる作りの可能性が高い。
    # なので、まずこの見た目のボックスをクリックしてドロップダウンを開いてから、
    # 改めて本物のinputを探す。
    try:
        page.get_by_text("モデルを検索または選択", exact=False).first.click(timeout=5000)
    except Exception:
        pass  # 見つからなくても、次のinput探索でカバーできる可能性があるので続行
    page.wait_for_timeout(500)

    # 検索ボックスを掴む。get_by_placeholder が反応しなかったため、
    # HTMLの属性を直接指定するCSSセレクターに変更（こちらの方が確実）。
    # 万一これも見つからない場合に備えて、候補をいくつか順番に試す。
    search_box = None
    for selector in [
        'input[placeholder*="モデル"]',
        'input[type="text"]:visible',
        '[role="combobox"] input',
        'input:visible',
    ]:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=5000)
            search_box = loc
            break
        except Exception:
            continue
    if search_box is None:
        raise RuntimeError("検索ボックスが見つからなかった")

    search_box.click()
    page.wait_for_timeout(300)

    # 既存の入力をキーボードで全選択→削除してから、1文字ずつ打つ
    # （.fill() だと一気に値が入りすぎて、絞り込み用のJSが反応しないことがあるため）
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(200)
    page.keyboard.type(model_label, delay=80)
    page.wait_for_timeout(1200)  # 候補が絞り込まれるのを待つ

    # 絞り込まれた候補の中から、同じ文字列のものをクリック
    page.get_by_text(model_label, exact=False).first.click(timeout=10000)
    page.wait_for_timeout(1500)  # Livewireの通信が返ってくるのを待つ

    html = page.content()
    stock, page_text = parse_store_html(html)
    return stock, page_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="初回ログイン用モード")
    args = parser.parse_args()

    if args.login:
        do_login()
        return

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_FILE)
        page = context.new_page()
        page.goto(BASE_URL)
        page.wait_for_timeout(2000)

        for model in MODEL_LIST:
            try:
                stock, page_text = fetch_model_stock(page, model)
                result[model] = stock
                print(f"[{model}] {len(stock)}店舗分取得")
                if not stock:
                    # 0件だった時は、読み取れた本文をテキストで保存する
                    # （正規表現を直すための調査用）
                    safe_name = model.replace(" ", "_").replace("/", "_")
                    with open(f"debug_text_{safe_name}.txt", "w", encoding="utf-8") as f:
                        f.write(page_text)
                    print(f"   → debug_text_{safe_name}.txt に本文を保存したよ")
            except Exception as e:
                print(f"⚠️ {model}: 失敗 {e}")
                # 失敗した瞬間の画面をスクショで保存（原因調査用）
                safe_name = model.replace(" ", "_").replace("/", "_")
                try:
                    page.screenshot(path=f"debug_{safe_name}.png")
                    print(f"   → debug_{safe_name}.png に画面を保存したよ")
                except Exception:
                    pass
            time.sleep(1)

        browser.close()

    # ログイン切れ等で全モデル0件だった場合は、既存の（正しい）ファイルを
    # 空データで上書きしないように、書き込み自体をスキップする
    total_stores = sum(len(v) for v in result.values())
    if total_stores == 0:
        print("⚠️ 全モデルで0件だった。ログイン状態が切れている可能性が高いので、")
        print("   既存のringonodorei_apple_stock.jsonは上書きせずに終了します。")
        print("   （GitHub Actionsで動かしてる場合はRINGONODOREI_AUTH_STATEの更新が必要）")
        return

    with open("ringonodorei_apple_stock.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("\n完了 → ringonodorei_apple_stock.json に保存したよ")


if __name__ == "__main__":
    main()
