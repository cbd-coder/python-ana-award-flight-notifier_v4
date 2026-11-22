"""ANA特典航空券: 回数制限付き検索・警告整理・往路選択・LINE通知。

既存の ana_award_search.py / ana_save_calendar.py / ana_multi_notify.py /
ana_multi_notify_v2.py / ana_line_notify.py と同じフォルダに保存する。
ANA側の利用条件を確認し、許される範囲で手動起動・少数回のみ使用する。
追加認証、アクセス制限、混雑は回避せず停止する。
"""
import asyncio
import os
import re
from datetime import date, timedelta

from playwright.async_api import async_playwright, Error as PlaywrightError

from ana_award_search import (
    ANA_URL, PROFILE_DIR, MAX_SEARCHES, SEARCH_DELAY,
    RESULT_TIMEOUT, SEARCH_INTERVAL_DAYS, NO_MATCH_CODE,
    add_months, click_visible, open_booking_page,
    login_ana, wait_for_search_form, fill_search_form,
    set_trip_dates, close_no_match_dialog,
)
from ana_save_calendar import extract_calendar, find_calendar_page
from ana_multi_notify import convert_dates, show_dates, find_matches, save_candidates, create_message
from ana_multi_notify_v2 import get_selected_outbound, remove_duplicates
from ana_line_notify import send_line_message

INFO_CODE = "W_A01P02_0004"
NO_DATE_CODE = "W_A01P02_0003"
CHECK_INTERVAL_SECONDS = 2
MAX_DIALOGS_PER_RESULT = 3
MAX_OUTBOUND_PER_CALENDAR = 7


async def ask(message):
    """ユーザー入力を非同期処理の実行を妨げずに受け取る。

    引数:
        message: 入力時に表示する案内文。
    戻り値:
        入力された文字列。
    """
    return await asyncio.to_thread(input, message)


async def visible_code(page, code):
    """画面上に指定したエラー・警告コードが表示されているか確認する。

    引数:
        page: 検索結果ページ。code: 確認対象のコード。
    戻り値:
        画面に表示されていればTrue、それ以外はFalse。
    """
    if page.is_closed():
        return False
    try:
        matches = page.get_by_text(re.compile(re.escape(code)))
        for i in range(min(await matches.count(), 12)):
            if await matches.nth(i).is_visible():
                return True
    except PlaywrightError:
        return False
    return False


async def has_calendar(page):
    """検索結果ページに空席カレンダーが表示されているか判定する。

    引数:
        page: 判定対象のページ。
    戻り値:
        カレンダーを確認できればTrue。
    """
    if page.is_closed():
        return False
    try:
        cells = page.locator("td.flightSelectParent")
        for i in range(min(await cells.count(), 40)):
            if await cells.nth(i).is_visible():
                return True
    except PlaywrightError:
        return False
    return False


async def has_congestion(page):
    """ページ上の混雑・アクセス制限に関するメッセージを検出する。

    引数:
        page: 確認対象のページ。
    戻り値:
        混雑・制限を検知した場合True。
    """
    if page.is_closed():
        return False
    try:
        body = (await page.locator("body").inner_text(timeout=2500))[:6000]
    except PlaywrightError:
        return False
    return any(word in body for word in (
        "アクセスが集中", "混み合って", "混雑して", "メンテナンス中",
        "しばらく時間をおいて", "アクセス制限", "しばらく経ってから",
    ))


async def close_known_warning(page, code):
    """警告コードを確認し、同じダイアログ内の「確認」を押す。"""
    if not await visible_code(page, code):
        return False
    print(f"案内 {code} を確認しました。")
    candidates = page.get_by_role("button", name="確認", exact=True)
    for i in range(min(await candidates.count(), 8)):
        item = candidates.nth(i)
        if await item.is_visible():
            await item.click(timeout=5000)
            for _ in range(10):
                if not await visible_code(page, code):
                    print("案内を閉じました。")
                    return True
                await asyncio.sleep(0.3)
            raise RuntimeError(f"案内 {code} が閉じられません。")
    # 明示的なボタンがない場合、安全のため停止
    raise RuntimeError(f"案内 {code} の『確認』ボタンを見つけられません。")


async def handle_known_warnings(page):
    """連続表示も想定。未知の警告には触れない。"""
    seen = []
    for _ in range(MAX_DIALOGS_PER_RESULT):
        if await has_congestion(page):
            raise RuntimeError("混雑・アクセス制限等を検出しました。")
        code = None
        if await visible_code(page, INFO_CODE):
            code = INFO_CODE
        elif await visible_code(page, NO_DATE_CODE):
            code = NO_DATE_CODE
        if code is None:
            break
        await close_known_warning(page, code)
        seen.append(code)
    if await visible_code(page, INFO_CODE) or await visible_code(page, NO_DATE_CODE):
        raise RuntimeError("複数の警告が続きました。安全のため停止します。")
    return seen


async def detect_outcome(context, booking_page):
    """空席があるとの断定はせず、『カレンダー表示』までを確認する。"""
    deadline = asyncio.get_running_loop().time() + RESULT_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            return "unknown", None
        for page in reversed(pages):
            if await has_congestion(page):
                return "congestion", page
            if await visible_code(page, NO_MATCH_CODE):
                return "no_match", page
            if await visible_code(page, INFO_CODE) or await visible_code(page, NO_DATE_CODE):
                await handle_known_warnings(page)
            if await has_calendar(page):
                return "calendar", page
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    return "unknown", booking_page


async def search_once_limited(context, booking_page, outbound):
    """指定した往路の日付で1回だけ検索し、画面の結果種別を判定する。

    引数:
        context: ブラウザコンテキスト。booking_page: 検索フォーム。outbound: 往路出発日。
    戻り値:
        検索結果の状態と表示ページを表す値。
    """
    inbound = await set_trip_dates(booking_page, outbound)
    print(f"検索：往路 {outbound} ／ 復路 {inbound}")
    button = booking_page.get_by_role("button", name="検索する", exact=True)
    if not await click_visible(button, "検索する"):
        raise RuntimeError("検索ボタンが見つかりません。")
    return await detect_outcome(context, booking_page)


async def click_outbound_date(page, target):
    """見出し『往路』より後、『復路』より前にある日付セルだけを選択。"""
    selected = await page.evaluate(
        r"""({month, day}) => {
            document.querySelectorAll('[data-ana-auto-outbound]')
              .forEach(el => el.removeAttribute('data-ana-auto-outbound'));
            let route = null;
            let count = 0;
            for (const el of document.querySelectorAll('body *')) {
                const t = (el.textContent || '').trim();
                if (t === '往路' || t === '復路') {
                    const innerSame = Array.from(el.children).some(
                        c => (c.textContent || '').trim() === t
                    );
                    if (!innerSame) route = (t === '往路' ? 'outbound' : 'return');
                }
                if (route !== 'outbound' || !el.matches('td.flightSelectParent')) continue;
                if (el.getClientRects().length === 0) continue;
                const label = el.querySelector('.simpleCalendarDateGroup label');
                const m = (label?.textContent || '').match(/(\d{1,2})月(\d{1,2})日/);
                if (!m || Number(m[1]) !== month || Number(m[2]) !== day) continue;
                const radio = el.querySelector('input[type="radio"]');
                if (!radio || radio.disabled) continue;
                count++;
                el.setAttribute('data-ana-auto-outbound', 'target');
            }
            return count;
        }""",
        {"month": target.month, "day": target.day},
    )
    if selected != 1:
        raise RuntimeError(
            f"往路 {target} の選択可能セルが{selected}件です。誤選択防止のため中止。"
        )
    cell = page.locator('td[data-ana-auto-outbound="target"]')
    radio = cell.locator('input[type="radio"]')
    # hidden inputの場合もあるため、まずラジオを通常クリック、失敗ならセル内の可視要素
    if await radio.is_visible():
        await radio.click(timeout=8000)
    else:
        await cell.locator(".insideBorder").click(timeout=8000)
    await asyncio.sleep(1)


async def verify_selected_outbound(page, expected):
    """選択状態の往路日付が予定の日付と一致することを検証する。

    引数:
        page: 空席カレンダー。expected: 選択したい往路日。
    戻り値:
        一致していればTrue、それ以外はFalse。
    """
    records = await get_selected_outbound(page)
    if len(records) != 1:
        print(f"選択済み往路が{len(records)}件：日付を確定できません。")
        return False
    actual = convert_dates(records, expected)
    if len(actual) != 1 or actual[0] != expected:
        print(f"選択日不一致：期待 {expected}／取得 {actual}")
        return False
    print(f"選択日検証OK：{expected}")
    return True


async def read_stable_returns(page, outbound):
    """更新直後の取り違えを減らす。2回連続で同じ表示なら採用。"""
    last = None
    for _ in range(8):
        if await has_congestion(page):
            raise RuntimeError("カレンダー確認中に混雑・制限表示を検出しました。")
        await handle_known_warnings(page)
        if not await verify_selected_outbound(page, outbound):
            return None
        result = await extract_calendar(page)
        returns = convert_dates(result["return"], outbound + timedelta(days=4))
        if last is not None and returns == last:
            return returns
        last = returns
        await asyncio.sleep(0.8)
    print("復路の表示が安定しませんでした。")
    return None


async def inspect_calendar(context, result_page, reference_date):
    """往路候補を選び、復路候補との希望日付差の組み合わせを調べる。

    引数:
        context: ブラウザコンテキスト。result_page: 検索結果ページ。reference_date: 年判定の基準日。
    戻り値:
        希望条件に一致した往復日程の一覧。
    """
    page = await find_calendar_page(context) or result_page
    await handle_known_warnings(page)
    raw = await extract_calendar(page)
    outbound_dates = convert_dates(raw["outbound"], reference_date)
    show_dates("表示中の往路空席候補", outbound_dates)
    if not outbound_dates:
        return []
    if len(outbound_dates) > MAX_OUTBOUND_PER_CALENDAR:
        print("候補が多すぎるため安全上この画面の自動選択を中止します。")
        return []

    candidates = []
    for idx, outbound in enumerate(outbound_dates, 1):
        print(f"\n===== 往路候補 {idx}/{len(outbound_dates)}：{outbound} =====")
        try:
            await handle_known_warnings(page)
            await click_outbound_date(page, outbound)
            if not await verify_selected_outbound(page, outbound):
                print("選択日を検証できないため、この候補を除外します。")
                continue
            returns = await read_stable_returns(page, outbound)
            if returns is None:
                print("復路情報を確実に確認できないため、この候補を除外します。")
                continue
            show_dates(f"往路 {outbound} の復路空席候補", returns)
            matches = find_matches(outbound, returns)
            print(f"条件一致：{len(matches)}件")
            candidates.extend(matches)
        except (PlaywrightError, RuntimeError, ValueError) as error:
            print(f"往路 {outbound} の確認を停止：{error}")
            print("画面状態が不明なため、このカレンダーの確認を終了します。")
            break
    return remove_duplicates(candidates)


async def main():
    """ANA検索の初期化から複数回検索、候補抽出、通知までを制御する旧V2版の入口。

    引数:
        なし。
    戻り値:
        なし（処理経過を標準出力に表示）。
    """
    print("===== ANA特典航空券：統合版 V2 =====")
    print("成田 ⇔ オークランド／大人2名／特典エコノミー")
    print(f"対象日付を最大{MAX_SEARCHES}回検索。画面操作は少数回に制限します。")
    print("カレンダーの『空席あり』は予約可能の保証ではありません。")

    if not os.getenv("ANA_PASSWORD"):
        print(".env の ANA_PASSWORD が読み込まれていません。")
        return
    start_date = add_months(date.today(), 2)
    print(f"初回往路検索日：{start_date}")
    candidates = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(ANA_URL, wait_until="domcontentloaded", timeout=60000)
            overseas = page.get_by_role("tab", name=re.compile("海外"))
            if await overseas.count():
                await click_visible(overseas, "海外タブ")
            award = page.get_by_role("tab", name="特典航空券", exact=True)
            if not await click_visible(award, "特典航空券タブ"):
                await ask("特典航空券タブを手動選択してEnter：")
            booking = await open_booking_page(context, page)
            await login_ana(booking, os.environ["ANA_PASSWORD"])
            await wait_for_search_form(booking)
            await fill_search_form(booking)

            for i in range(MAX_SEARCHES):
                target = start_date + timedelta(days=SEARCH_INTERVAL_DAYS * i)
                print(f"\n===== 検索 {i + 1}/{MAX_SEARCHES}：{target} =====")
                status, result_page = await search_once_limited(context, booking, target)
                print(f"検索結果：{status}")
                if status == "no_match":
                    if result_page != booking:
                        print("エラー画面と検索フォームが異なるため停止します。")
                        break
                    await close_no_match_dialog(result_page)
                    if i + 1 < MAX_SEARCHES:
                        print(f"{SEARCH_DELAY}秒後に次の日付へ進みます。")
                        await asyncio.sleep(SEARCH_DELAY)
                    continue
                if status == "calendar":
                    candidates = await inspect_calendar(context, result_page, target)
                    # この版ではカレンダー確認後、追加の検索リクエストは行わない
                    break
                if status == "congestion":
                    print("混雑・メンテナンス等により停止します。")
                else:
                    print("判定不能のため停止します。")
                break

            print(f"\n最終候補：{len(candidates)}件")
            if candidates:
                save_candidates(candidates)
                message = create_message(candidates)
                print("\n===== LINE通知文 =====\n" + message)
                answer = (await ask("LINEに送信しますか？ (y/n)：")).strip().lower()
                if answer == "y":
                    if send_line_message(message):
                        print("LINE通知完了。")
                    else:
                        print("LINE送信に失敗。CSVは保存済みです。")
                else:
                    print("通知をキャンセル。CSVは保存済みです。")
            else:
                print("新規候補なし。CSV上書きとLINE通知は行いません。")
            await ask("ブラウザを確認してEnterで終了：")
        except (PlaywrightError, RuntimeError, ValueError) as error:
            print(f"停止：{error}")
            await ask("画面を確認してEnterで終了：")
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
