"""ANA特典航空券：提出用（最大3回、7日間隔、候補日をLINE通知）。

既存の ana_award_search.py、ana_auto_search_v2.py、ana_multi_notify.py、
ana_multi_notify_v2.py、ana_save_calendar.py、ana_line_notify.py を変更せず利用。

注意：ANAの画面構造は変化します。結果画面から検索フォームへ戻れない場合や
エラー・アクセス制限を検出した場合は、自動再試行せず停止します。
カレンダー上の空席候補は2名分の実際の予約可否を保証しません。
"""
import asyncio
import os
import re
from datetime import date, timedelta, datetime

from playwright.async_api import async_playwright, Error as PlaywrightError

from ana_award_search import (
    ANA_URL,
    PROFILE_DIR,
    SEARCH_DELAY,
    SEARCH_INTERVAL_DAYS as OLD_SEARCH_INTERVAL_DAYS,
    add_months,
    click_visible,
    open_booking_page,
    login_ana,
    wait_for_search_form,
    fill_search_form,
    close_no_match_dialog,
    is_no_match_error,
    select_airport,
)
import ana_award_search as award_search

from ana_auto_search_v2 import (
    ask,
    has_congestion,
    search_once_limited,
    click_outbound_date,
    verify_selected_outbound,
    read_stable_returns,
    handle_known_warnings,
    MAX_OUTBOUND_PER_CALENDAR,
)
from ana_multi_notify_v2 import remove_duplicates
from ana_multi_notify import convert_dates, show_dates, find_matches
from ana_save_calendar import extract_calendar, find_calendar_page
from ana_multi_notify import save_candidates, create_message
from ana_line_notify import send_line_message

# 確実に識別できた「検索条件へ戻る」操作だけを対象にする。
RETURN_LABELS = (
    "検索条件を変更",
    "検索条件の変更",
    "条件を変更",
    "検索条件を修正",
    "検索をやり直す",
)
RETURN_TIMEOUT_MS = 12000
MAX_BACK_ATTEMPTS = 1
SEARCH_INTERVAL_DAYS = 7
MAX_SEARCHES = 3  # 1回の実行で検索は3回まで
MAX_REFERENCE_DISPLAY = 15  # LINEで表示する参考候補の上限
MAX_CALENDAR_MOVES = 5

# 日付カレンダーの表示月をDOMから読み取り、前後の方向を決める。
# ANAのHTML構造が想定と違えば停止し、誤った日付を選択しない。
async def calendar_months(page):
    """HTMLの全月を列挙する（この一覧はページ移動しても変化しない）。"""
    items = await page.evaluate(r"""() => {
        return Array.from(document.querySelectorAll('[id^="calsec"]'))
            .map(e => {
                const m = (e.innerText || '').match(/(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月/);
                return m ? {id:e.id, year:Number(m[1]), month:Number(m[2])} : null;
            }).filter(Boolean);
    }""")
    return items


async def active_calendar_months(page):
    """DOM内の全月ではなく、画面上でクリック可能な月を確認する。

    ANAの画面では全月がDOMに残るため is_visible() のみでは不十分。
    実際の座標にある要素が、その月の子孫かをelementFromPointで確認する。
    """
    return await page.evaluate(r"""() => {
        const result = [];
        const all = document.querySelectorAll('[id^="calsec"]');
        for (const section of all) {
            const match = (section.innerText || '').match(/(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月/);
            if (!match) continue;
            // 表示領域のヒットテスト。見出しや日付リンクなど複数地点を調べる。
            const candidates = [section, ...section.querySelectorAll('a')].slice(0, 30);
            let onScreen = false;
            for (const el of candidates) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
                const top = document.elementFromPoint(x, y);
                if (top && (section === top || section.contains(top))) {
                    onScreen = true;
                    break;
                }
            }
            if (onScreen) result.push({id:section.id, year:Number(match[1]), month:Number(match[2])});
        }
        return result;
    }""")


async def safe_navigate_calendar(page, target_date):
    """カレンダーの実際の画面表示を確認しながら目的月を選ぶ。

    重要: ANAの全月 calsec0..12 は常時DOMにあり、月送りでも
    innerTextは変わらない。実際にクリック可能な月の集合で比較する。
    """
    target = (target_date.year, target_date.month)
    all_months = await calendar_months(page)
    targets = [item for item in all_months if (item['year'],item['month']) == target]
    if len(targets) != 1:
        raise RuntimeError(f"目的月 {target_date:%Y-%m} がカレンダーDOMに一意にありません。")

    for step in range(MAX_CALENDAR_MOVES + 1):
        active = await active_calendar_months(page)
        shown = sorted({(m['year'], m['month']) for m in active})
        text = ', '.join(f'{y}-{m:02d}' for y,m in shown) or '判定不可'
        print('実際に操作可能なカレンダー表示月：', text)

        if target in shown:
            section = page.locator('#' + targets[0]['id'])
            if await section.count() != 1:
                raise RuntimeError('対象月の要素を一意に取得できません。')
            return section

        if not shown:
            raise RuntimeError('画面上の3か月を安全に特定できません。追加クリックせず停止します。')
        if step >= MAX_CALENDAR_MOVES:
            break

        if target < shown[0]:
            label = '前の3ヶ月'
        elif target > shown[-1]:
            label = '次の3ヶ月'
        else:
            raise RuntimeError('目的月が表示範囲内にあるのに選べません。')

        # 操作可能なボタンは１つだけであることを確認
        loc = page.get_by_role('button', name=label, exact=True)
        buttons = []
        for idx in range(min(await loc.count(), 10)):
            btn = loc.nth(idx)
            if await btn.is_visible() and await btn.is_enabled():
                buttons.append(btn)
        if len(buttons) != 1:
            raise RuntimeError(f'「{label}」の操作可能ボタン数が{len(buttons)}件のため停止します。')

        print(f'目的月 {target_date:%Y-%m} に向けて「{label}」を1回クリック')
        await buttons[0].click(timeout=8000)
        before = set(shown)
        changed = False
        for _ in range(16):
            await asyncio.sleep(0.25)
            updated = await active_calendar_months(page)
            after = {(m['year'],m['month']) for m in updated}
            if after and after != before:
                changed = True
                break
        if not changed:
            raise RuntimeError('クリック後、画面上で操作可能な月の変化を確認できません。停止します。')

    raise RuntimeError(f'目的月 {target_date:%Y-%m} まで移動できませんでした。')


# 元の set_trip_dates/select_date は内部で navigate_calendar を参照する。
# 既存ファイルを変更せずV3.9の安全な月移動に切り替える。
award_search.navigate_calendar = safe_navigate_calendar



async def is_form_ready(page):
    """検索フォームの出発地・到着地・日付欄が見えているか確認。"""
    if page is None or page.is_closed():
        return False
    try:
        names = ("出発地", "到着地", "往路出発日", "復路出発日")
        for name in names:
            loc = page.get_by_role("textbox", name=name, exact=True)
            if await loc.count() == 0 or not await loc.first.is_visible():
                return False
        return True
    except PlaywrightError:
        return False


async def wait_form(page, seconds=12):
    """検索フォーム復帰時、履歴から再表示された既知の古いエラーを閉じる。"""
    for _ in range(seconds * 2):
        if await has_congestion(page):
            raise RuntimeError("混雑・アクセス制限・メンテナンスを検出したため停止します。")

        # 今回の実画面で確認できたケース：履歴を戻ると以前の
        # E_A01P01_0006 ダイアログまで復元される。
        # 未知の警告やログイン・アクセス制限には触れない。
        if await is_no_match_error(page):
            print("以前の検索の E_A01P01_0006 が再表示されました。")
            await close_no_match_dialog(page)
            print("古いエラーを閉じて検索フォームを確認します。")

        if await is_form_ready(page):
            return True
        await asyncio.sleep(0.5)
    return False


async def click_return_button(page):
    """明示的な『条件変更』リンク/ボタンが1件だけあればクリック。"""
    if page is None or page.is_closed():
        return False
    for label in RETURN_LABELS:
        found = []
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=label, exact=True)
            for i in range(min(await loc.count(), 8)):
                item = loc.nth(i)
                if await item.is_visible() and await item.is_enabled():
                    found.append(item)
        if len(found) == 1:
            print(f"検索条件に戻るボタンをクリック：{label}")
            await found[0].click(timeout=8000)
            return True
        if len(found) > 1:
            raise RuntimeError(f"『{label}』が複数見つかりました。誤操作防止のため停止します。")
    return False


async def return_to_search_form(context, booking_page, result_page):
    """検索結果から戻る。確実な復帰ができないときは打ち切る。"""
    print("\n次の日付を検索するため、検索条件画面へ戻ります（古いエラーの再表示にも対応）。")

    # 結果が別タブの場合、元のフォームが残っている可能性がある。
    if result_page is not booking_page and await wait_form(booking_page, seconds=3):
        if not result_page.is_closed():
            await result_page.close()
        print("元の検索タブに戻りました。")
        return booking_page

    # まずサイト側の明示的な「条件変更」操作を探す。
    for page in (result_page, booking_page):
        if page is None or page.is_closed():
            continue
        if await wait_form(page, seconds=3):
            print("検索フォームを確認しました。")
            return page
        if await click_return_button(page):
            if await wait_form(page):
                if result_page is not page and not result_page.is_closed():
                    await result_page.close()
                print("条件変更画面への復帰に成功しました。")
                return page
            # ボタンが新しいタブを開くケースは一度だけチェック。
            for new_page in reversed(context.pages):
                if not new_page.is_closed() and await wait_form(new_page, seconds=3):
                    print("新しいタブの検索フォームを確認しました。")
                    return new_page
            raise RuntimeError("条件変更ボタン後に検索フォームを確認できませんでした。")

    # 条件変更ボタンがないケースのみ、ブラウザ履歴を1回戻して確認する。
    page = result_page if result_page and not result_page.is_closed() else booking_page
    if page is None or page.is_closed():
        raise RuntimeError("検索結果のブラウザタブが閉じられています。")
    for _ in range(MAX_BACK_ATTEMPTS):
        print("条件変更ボタンがないため、ブラウザ履歴を1回戻します。")
        await page.go_back(wait_until="domcontentloaded", timeout=20000)
        if await wait_form(page):
            print("古いエラーがない検索フォームに戻りました。")
            return page
    raise RuntimeError(
        "検索フォームへ安全に戻れませんでした。追加検索を中止します。"
        " 画面の表示とPowerShellのログを確認してください。"
    )


async def read_adult_counter(page):
    """大人欄の実際の選択値をDOMから取得する。

    旧版は人数欄のinner_textを読んだため、ドロップダウン候補の
    「0 1 2 ... 9」まで連結してしまった。今回は選択中のoption、
    input値、aria-valuenowのみを信頼し、判定不能なら停止する。
    戻り値：(人数またはNone, 特定した大人欄のlocator)
    """
    labels = page.get_by_text("16歳以上", exact=True)
    visible_labels = []
    for i in range(min(await labels.count(), 12)):
        item = labels.nth(i)
        if await item.is_visible():
            visible_labels.append(item)
    if len(visible_labels) != 1:
        raise RuntimeError(
            f"『16歳以上』の見出しが{len(visible_labels)}件です。"
            "大人欄を特定できないため停止します。"
        )

    label = visible_labels[0]
    group = None
    # まず「大人」の＋／－だけを含む最小の親要素を探す
    node = label
    for _ in range(7):
        node = node.locator("xpath=..")
        plus = node.get_by_text("+", exact=True)
        minus = node.get_by_text("-", exact=True)
        visible_plus = [plus.nth(i) for i in range(min(await plus.count(), 12))
                        if await plus.nth(i).is_visible()]
        visible_minus = [minus.nth(i) for i in range(min(await minus.count(), 12))
                         if await minus.nth(i).is_visible()]
        if len(visible_plus) == 1 and len(visible_minus) == 1:
            # 他の年齢区分が混ざる大きなコンテナは使わない
            text = await node.inner_text(timeout=4000)
            if any(s in text for s in ("12-15歳", "2-11歳", "0-1歳")):
                break
            group = node
            break
    if group is None:
        raise RuntimeError("大人欄だけを含む要素を特定できませんでした。")

    # 実際の選択状態を持つフォーム要素を優先する。
    # optionの全テキストではなく「選択済みoption」だけを読む。
    info = await group.evaluate(r"""element => {
        const isInt = v => /^\d+$/.test(String(v || '').trim());
        const found = [];
        const selects = [...element.querySelectorAll('select')];
        for (const s of selects) {
            const option = s.selectedOptions && s.selectedOptions[0];
            if (!option) continue;
            const raw = String(option.value || '').trim();
            const text = String(option.textContent || '').trim();
            if (isInt(raw)) found.push({kind:'select', value:Number(raw)});
            else if (isInt(text)) found.push({kind:'select-text', value:Number(text)});
        }
        // selectがなく、数値入力欄がある場合
        for (const inp of element.querySelectorAll('input')) {
            const type = (inp.getAttribute('type') || '').toLowerCase();
            if (type === 'hidden' || type === 'password') continue;
            const val = String(inp.value || '').trim();
            if ((type === 'number' || inp.getAttribute('role') === 'spinbutton') && isInt(val)) {
                found.push({kind:'input', value:Number(val)});
            }
        }
        for (const e of element.querySelectorAll('[aria-valuenow]')) {
            const val = e.getAttribute('aria-valuenow');
            if (isInt(val)) found.push({kind:'aria-valuenow', value:Number(val)});
        }
        return found;
    }""")

    values = {int(item["value"]) for item in info if 0 <= int(item["value"]) <= 9}
    if len(values) == 1:
        value = values.pop()
        print(f"大人人数：DOMの選択値から{value}名を確認しました。")
        return value, group

    # 候補の全数字を誤読しないよう、不明なら判定不能とする
    print(f"大人人数の選択値を特定できません（検出値：{info}）。")
    return None, group


async def ensure_adults_two(page):
    """1名なら大人欄の＋を1回だけ押し、必ず2名への変化を確認する。"""
    current, group = await read_adult_counter(page)
    if current == 2:
        print("大人人数：2名を確認しました。")
        return
    if current != 1:
        raise RuntimeError(
            "大人人数を安全に特定できません。操作せず停止しました。"
        )

    plus = group.get_by_text("+", exact=True)
    visible_plus = []
    for i in range(min(await plus.count(), 12)):
        item = plus.nth(i)
        if await item.is_visible() and await item.is_enabled():
            visible_plus.append(item)
    if len(visible_plus) != 1:
        raise RuntimeError("大人欄の＋ボタンを一つに特定できません。")

    print("大人人数：1名 → ＋を1回クリックして2名に変更します。")
    await visible_plus[0].click(timeout=8000)
    await asyncio.sleep(0.5)
    actual, _ = await read_adult_counter(page)
    if actual != 2:
        raise RuntimeError(
            f"大人人数を2名と確認できません（取得結果：{actual}）。"
            "これ以上クリックせず停止します。"
        )
    print("大人人数の自動設定：2名を確認しました。")


async def ensure_award_economy(page):
    """表示中のプルダウンから選択クラスを確認する。"""
    selects = page.locator("select")
    matching = []
    for i in range(min(await selects.count(), 20)):
        field = selects.nth(i)
        if not await field.is_visible():
            continue
        options = field.locator("option")
        option_texts = []
        for j in range(min(await options.count(), 30)):
            option_texts.append((await options.nth(j).inner_text()).strip())
        if any("特典エコノミー" in s.replace(" ", "") for s in option_texts):
            matching.append((field, option_texts))
    if len(matching) != 1:
        raise RuntimeError(
            f"クラス選択欄の特定結果：{len(matching)}件。"
            "『特典エコノミー』を安全に確認できないため停止します。"
        )
    field, _ = matching[0]
    selected = (await field.locator("option:checked").inner_text()).strip()
    if "特典エコノミー" not in selected.replace(" ", ""):
        raise RuntimeError(
            f"現在のクラスは{selected!r}です。"
            "誤設定防止のため停止します。"
        )
    print("クラス：特典エコノミーを確認しました。")


async def check_search_form_state(page):
    """検索フォームの復帰後、空港・人数・クラスを再確認する。"""
    expected_airports = (
        ("出発地", "nrt", "東京.*成田", ("nrt", "成田")),
        ("到着地", "akl", "オークランド", ("akl", "オークランド")),
    )
    needs_restore = []
    for name, code, option, accepted in expected_airports:
        field = page.get_by_role("textbox", name=name, exact=True)
        if await field.count() != 1 or not await field.is_visible():
            raise RuntimeError(f"『{name}』欄を1件に特定できません。")
        value = (await field.input_value()).strip()
        if not value:
            needs_restore.append((name, code, option))
        elif not any(keyword.lower() in value.lower() for keyword in accepted):
            raise RuntimeError(
                f"『{name}』が想定外の値です：{value!r}。誤検索を防ぐため停止します。"
            )

    if needs_restore:
        print("検索フォームの一部が初期化されたため、空港欄を再入力します。")
        for name, code, option in needs_restore:
            print(f"『{name}』を再入力します。")
            await select_airport(page, name, code, option)
    else:
        print("出発地・到着地の入力が保持されています。")

    for name, code, option, accepted in expected_airports:
        value = (await page.get_by_role("textbox", name=name, exact=True)
                 .input_value()).strip()
        if not value or not any(t.lower() in value.lower() for t in accepted):
            raise RuntimeError(f"『{name}』の再設定を確認できません。")

    await ensure_adults_two(page)
    await ensure_award_economy(page)
    print("検索条件を検証しました。次の検索を行います。")


async def inspect_calendar_with_references(context, result_page, reference_date):
    """既存の空席候補取得と同じ手順で、希望日数以外の表示も記録する。

    参考候補は「カレンダー上の往路・復路表示」の組み合わせに過ぎない。
    実際に購入可能な旅程とは判定しない。
    """
    page = await find_calendar_page(context) or result_page
    await handle_known_warnings(page)
    raw = await extract_calendar(page)
    outbound_dates = convert_dates(raw["outbound"], reference_date)
    show_dates("表示中の往路空席候補", outbound_dates)
    if not outbound_dates:
        return [], []
    if len(outbound_dates) > MAX_OUTBOUND_PER_CALENDAR:
        print("候補が多すぎるため、この画面の自動選択を中止します。")
        return [], []

    matches_all = []
    references = []
    for index, outbound in enumerate(outbound_dates, 1):
        print(f"\n===== 往路候補 {index}/{len(outbound_dates)}：{outbound} =====")
        try:
            await handle_known_warnings(page)
            await click_outbound_date(page, outbound)
            if not await verify_selected_outbound(page, outbound):
                print("選択日を確認できないため、この往路は除外します。")
                continue
            returns = await read_stable_returns(page, outbound)
            if returns is None:
                print("復路情報を確認できないため、この往路は除外します。")
                continue
            show_dates(f"往路 {outbound} の復路空席候補", returns)
            matches = find_matches(outbound, returns)
            matches_all.extend(matches)
            print(f"条件一致：{len(matches)}件")

            for inbound in returns:
                days = (inbound - outbound).days
                if days >= 0 and not 4 <= days <= 5:
                    references.append({
                        "outbound": outbound.isoformat(),
                        "inbound": inbound.isoformat(),
                        "days": days,
                    })
        except (PlaywrightError, RuntimeError, ValueError) as error:
            print(f"往路 {outbound} の確認を停止：{error}")
            print("画面状態が不明なため、このカレンダーの確認を終了します。")
            break
    return remove_duplicates(matches_all), references


def format_references(references):
    """別の日数でも見えた往復日付を重複なしで文字列にする。"""
    unique = {(r["outbound"], r["inbound"], r["days"]) for r in references}
    ordered = sorted(unique)
    if not ordered:
        return "参考候補：今回の確認範囲ではありませんでした。"
    lines = ["【参考：4～5日差以外の空席表示】"]
    for out, back, days in ordered[:MAX_REFERENCE_DISPLAY]:
        lines.append(f"往路 {out} → 復路 {back}（出発日差{days}日）")
    if len(ordered) > MAX_REFERENCE_DISPLAY:
        lines.append(f"ほか{len(ordered) - MAX_REFERENCE_DISPLAY}件は省略")
    lines.append("※それぞれのカレンダーに空席表示があった日付です。往復旅程としての予約可否は未確認です。")
    return "\n".join(lines)


async def main():
    """提出版の全体処理を管理する。最大3回の検索、候補分類、LINE通知を実行する。

    引数:
        なし。
    戻り値:
        なし（結果を画面に表示し、必要に応じて通知する）。
    """
    print("===== ANA特典航空券・空席候補通知【提出版・参考日程付き】 =====")
    print("成田 ⇔ オークランド / 大人2名 / 特典エコノミー")
    print(f"最大検索回数：{MAX_SEARCHES}回")
    print(f"検索基準日の間隔：{SEARCH_INTERVAL_DAYS}日")
    print("空席カレンダーが表示されても、次の日付の検索を試みます。")
    print("アクセス制限・混雑・不明な画面では停止します。")
    print("空席表示は予約可否・現地宿泊数の保証ではありません。")
    print("本版は起動時にのみ検索します。自動再起動・常時監視はしません。")

    password = os.getenv("ANA_PASSWORD")
    if not password:
        print("ANA_PASSWORD を読み込めません。既存の .env を確認してください。")
        return

    executed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    start_date = add_months(date.today(), 2)
    print(f"最初の検索基準日：{start_date}")
    all_candidates = []
    all_references = []
    searched = 0
    completed_all = True
    stop_reason = None

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(ANA_URL, wait_until="domcontentloaded", timeout=60000)

            overseas = page.get_by_role("tab", name=re.compile("海外"))
            if await overseas.count() > 0:
                await click_visible(overseas, "海外タブ")
            award = page.get_by_role("tab", name="特典航空券", exact=True)
            if not await click_visible(award, "特典航空券タブ"):
                await ask("特典航空券タブを選択したらEnter：")

            booking = await open_booking_page(context, page)
            await login_ana(booking, password)
            await wait_for_search_form(booking)
            await fill_search_form(booking)
            # 初回入力も大人2名・クラスの表示で確認する。
            await ensure_adults_two(booking)
            await ensure_award_economy(booking)

            for index in range(MAX_SEARCHES):
                target = start_date + timedelta(days=SEARCH_INTERVAL_DAYS * index)
                print(f"\n========== 検索 {index + 1}/{MAX_SEARCHES} ==========")
                print(f"基準の往路：{target}")
                if not await is_form_ready(booking):
                    completed_all = False
                    stop_reason = "検索フォームが表示されていません。"
                    break
                if index > 0:
                    await check_search_form_state(booking)
                if await has_congestion(booking):
                    completed_all = False
                    stop_reason = "混雑・制限を検出しました。"
                    break

                searched += 1
                status, result_page = await search_once_limited(context, booking, target)
                print(f"検索結果：{status}")

                if status == "no_match":
                    # 明示的に旅程なしと判定できた場合のみ、次の日付へ進む。
                    if result_page is not booking:
                        completed_all = False
                        stop_reason = "検索フォームとエラーの画面が異なります。"
                        break
                    await close_no_match_dialog(result_page)

                elif status == "calendar":
                    # 表示された空席候補を評価して集約する。
                    current, reference_pairs = await inspect_calendar_with_references(
                        context, result_page, target
                    )
                    print(f"この検索画面で見つかった候補：{len(current)}件")
                    print(f"希望日数以外の参考候補：{len(reference_pairs)}件")
                    all_candidates.extend(current)
                    all_references.extend(reference_pairs)

                else:
                    completed_all = False
                    stop_reason = (
                        "混雑・アクセス制限が検出されました。"
                        if status == "congestion" else
                        "検索結果を安全に判定できませんでした。"
                    )
                    break

                if index + 1 == MAX_SEARCHES:
                    continue

                # no_matchではダイアログが閉じてそのままフォームに戻ることがある。
                if status == "calendar":
                    try:
                        booking = await return_to_search_form(
                            context, booking, result_page
                        )
                    except (PlaywrightError, RuntimeError) as error:
                        completed_all = False
                        stop_reason = str(error)
                        break
                elif not await is_form_ready(booking):
                    completed_all = False
                    stop_reason = "空席なしの画面から検索フォームに戻れませんでした。"
                    break

                print(f"次の検索まで{SEARCH_DELAY}秒待機します。")
                await asyncio.sleep(SEARCH_DELAY)

            candidates = remove_duplicates(all_candidates)
            reference_text = format_references(all_references)
            print("\n========== 最終結果 ==========")
            print(f"実施した検索：{searched}/{MAX_SEARCHES}回")
            print(f"重複除去後の候補：{len(candidates)}件")
            if not completed_all:
                print("一部未確認の検索があります。")
                print(f"停止理由：{stop_reason}")
            if candidates:
                save_candidates(candidates)
                print("CSVに保存しました。")
                message = create_message(candidates)
                summary = (
                    f"検索日時：{executed_at}\n"
                    f"検索回数：{searched}/{MAX_SEARCHES}回\n"
                    "検索方式：前後3日比較／7日間隔\n"
                    "※出発日の差4～5日は現地の宿泊数を保証しません。\n"
                    "※往復として予約できること、2名分の空席、便名・料金は未確認です。\n"
                )
                if not completed_all:
                    summary += "【注意】予定した検索の一部は未完了です。\n"
                message = summary + "\n" + message + "\n\n" + reference_text
                if len(message) > 4900:
                    print("LINEメッセージが長すぎるため通知しません。CSVを確認してください。")
                    message = None
                if message is not None:
                    print("\n===== LINE通知内容 =====\n" + message)
                    answer = (await ask("LINEへ送信しますか？ (y/n)：")).strip().lower()
                    if answer == "y":
                        if send_line_message(message):
                            print("LINEへの通知が完了しました。")
                        else:
                            print("LINE送信に失敗しました。CSVは保存済みです。")
                    else:
                        print("LINE通知はキャンセルしました。CSVは保存済みです。")
            else:
                print("今回確認した範囲では条件一致候補なし。")
                print("既存のCSVは上書きしません（過去の結果を最新と誤認しないでください）。")
                no_result = (
                    "【ANA特典航空券・検索結果】\n"
                    f"検索日時：{executed_at}\n"
                    f"検索回数：{searched}/{MAX_SEARCHES}回\n"
                    "希望の出発日差4～5日の候補は見つかりませんでした。\n"
                    + reference_text + "\n"
                    "※実際の現地宿泊数・便名・必要マイル数・税金等は未確認です。\n"
                    "※検索できた範囲の結果で、今後の空席を保証しません。"
                )
                if not completed_all:
                    no_result += "\n※一部の検索が未完了です。"
                print("\n===== LINE通知内容 =====\n" + no_result)
                if len(no_result) > 4900:
                    raise RuntimeError("LINE通知が長すぎます。送信せず停止します。")
                answer = (await ask("希望日数の候補なし・参考日程をLINEへ送りますか？ (y/n)：")).strip().lower()
                if answer == "y":
                    print("LINE送信結果：", "成功" if send_line_message(no_result) else "失敗")
            await ask("ブラウザを確認したらEnterで終了：")

        except (PlaywrightError, RuntimeError, ValueError) as error:
            print(f"\n処理を停止しました：{error}")
            try:
                diagnostic = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "ana_submission_error.png"
                )
                if booking is not None and not booking.is_closed():
                    await booking.screenshot(path=diagnostic, full_page=False)
                    print(f"画面の画像を保存しました：{diagnostic}")
                    print("共有する前に個人情報が写っていないか確認してください。")
            except Exception as screenshot_error:
                print(f"画面記録には失敗しました：{screenshot_error}")
            if all_candidates:
                print(f"停止前に確認できた候補：{len(remove_duplicates(all_candidates))}件")
                print("エラー終了のため自動通知は行いません。")
            await ask("画面を確認したらEnterで終了：")
        finally:
            await context.close()


async def calendar_only_test(day_text):
    """ANA検索を実行せず、日付カレンダーの月移動だけを検証する。"""
    target = date.fromisoformat(day_text)
    print('カレンダー月移動だけをテストします。ANAの検索・LINE通知は行いません。')
    async with async_playwright() as playwright:
        ctx = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False,
            viewport={'width':1280, 'height':900})
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            print('ANAの予約検索画面を手動で開いて、日付カレンダーを表示してください。')
            await ask('カレンダーを表示したらEnter：')
            # 検索画面を開いているタブを特定する
            matches = []
            for p in ctx.pages:
                if p.is_closed(): continue
                if await p.locator('[id^="calsec"]').count() >= 3:
                    matches.append(p)
            if len(matches) != 1:
                print('日付カレンダーのあるタブを1件に特定できません。')
                return
            section = await safe_navigate_calendar(matches[0], target)
            text = (await section.inner_text()).strip()[:90]
            print('月移動テスト成功：', target.strftime('%Y-%m'), '/', text)
            print('※日付のクリックやフライト検索はしていません。')
        except Exception as error:
            print('月移動テスト失敗：', error)
        finally:
            await ask('確認できたらEnterで終了：')
            await ctx.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--calendar-test':
        asyncio.run(calendar_only_test(sys.argv[2]))
    else:
        asyncio.run(main())
