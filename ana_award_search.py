
import asyncio
import calendar
import os
import re

from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from playwright.async_api import (
    async_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


# ==========================================
# 基本設定
# ==========================================

ANA_URL = "https://www.ana.co.jp/"

PROFILE_DIR = Path.home() / "ana_automation_profile"

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

DEPARTURE = "nrt"
ARRIVAL = "akl"

STAY_DAYS = 4

SEARCH_INTERVAL_DAYS = 15

# 最初の検索を含めて最大6回
MAX_SEARCHES = 6

# 検索間の待機時間（秒）
SEARCH_DELAY = 12

# 検索結果を待つ最大時間（秒）
RESULT_TIMEOUT = 45

NO_MATCH_CODE = "E_A01P01_0006"


# ==========================================
# 日付計算
# ==========================================

def add_months(original_date, months):

    """指定日から所定の月数だけ移動した日付を計算する。移動先の月末日も考慮する。

    引数:
        original_date: 起点の日付。months: 加減する月数。
    戻り値:
        移動後のdate型の日付。
    """
    month_index = (
        original_date.year * 12
        + original_date.month - 1
        + months
    )

    year, month_index = divmod(
        month_index, 12
    )

    month = month_index + 1

    day = min(
        original_date.day,
        calendar.monthrange(year, month)[1]
    )

    return date(year, month, day)


# ==========================================
# 表示中の要素をクリック
# ==========================================

async def click_visible(
    locator,
    description,
    timeout=15000
):

    """指定した要素のうち、実際に表示されているものをクリックする。

    引数:
        locator: 操作対象。description: ログ表示名。timeout: 待機時間（ミリ秒）。
    戻り値:
        クリック処理の成否を示す値。
    """
    count = await locator.count()

    for i in range(count):

        item = locator.nth(i)

        if await item.is_visible():

            await item.click(
                timeout=timeout
            )

            print(f"成功：{description}")

            return True

    return False


# ==========================================
# 空港を選択
# ==========================================

async def select_airport(
    page,
    field_name,
    code,
    option_name
):

    """出発地・到着地の入力欄から空港コードを指定する。

    引数:
        page: 検索フォーム。field_name: 項目名。code: 空港コード。option_name: 候補表示名。
    戻り値:
        なし。
    """
    print(f"{field_name}を設定します。")

    field = page.get_by_role(
        "textbox",
        name=field_name,
        exact=True
    )

    await field.click(
        timeout=15000
    )

    await field.fill(
        code,
        timeout=15000
    )

    option = page.get_by_role(
        "option",
        name=re.compile(
            option_name,
            re.I
        )
    )

    await option.first.wait_for(
        state="visible",
        timeout=15000
    )

    await option.first.click(
        timeout=15000
    )

    print(
        f"設定完了：{field_name} = "
        f"{code.upper()}"
    )


# ==========================================
# 検索フォームの基本入力
# ==========================================

async def fill_search_form(page):

    """ANA特典航空券検索フォームに基本条件を設定する。

    引数:
        page: ANAの検索フォーム。
    戻り値:
        なし。
    """
    print()
    print("===== 基本条件を入力 =====")

    await select_airport(
        page,
        "出発地",
        DEPARTURE,
        "東京.*成田"
    )

    await select_airport(
        page,
        "到着地",
        ARRIVAL,
        "オークランド"
    )

    print("クラス：特典エコノミー")

    plus_button = page.get_by_text(
        "+",
        exact=True
    ).first

    await plus_button.click(
        timeout=15000
    )

    print("大人人数を2名に変更しました。")


# ==========================================
# カレンダーの対象月を探す
# ==========================================

async def find_calendar_section(
    page,
    target_date
):

    """指定日が属する月のカレンダー表示領域を探す。

    引数:
        page: 日付選択画面。target_date: 指定日。
    戻り値:
        該当するカレンダー要素、または見つからない場合の値。
    """
    for index in range(3):

        section = page.locator(
            f"#calsec{index}"
        )

        if await section.count() == 0:
            continue

        if not await section.is_visible():
            continue

        content = await section.inner_text()

        pattern = (
            rf"{target_date.year}\s*年\s*"
            rf"0?{target_date.month}\s*月"
        )

        if re.search(
            pattern,
            content
        ):

            return section

    return None


# ==========================================
# カレンダーを移動
# ==========================================

async def navigate_calendar(
    page,
    target_date
):

    """カレンダーを月送りして指定日を選択できる月へ移動する。

    引数:
        page: 日付選択画面。target_date: 指定日。
    戻り値:
        目的月のカレンダー領域。
    """
    for attempt in range(9):

        section = await find_calendar_section(
            page,
            target_date
        )

        if section is not None:

            return section

        print(
            f"対象月 {target_date:%Y-%m} "
            "を探しています。"
        )

        next_button = page.get_by_text(
            "次の3ヶ月"
        )

        moved = await click_visible(
            next_button,
            "次の3ヶ月"
        )

        if not moved:

            raise RuntimeError(
                "カレンダーの月移動に失敗しました。"
            )

        await page.wait_for_timeout(600)

    raise RuntimeError(
        f"対象月 {target_date:%Y-%m} "
        "を表示できません。"
    )


# ==========================================
# 日付選択
# ==========================================

async def select_date(
    page,
    field_name,
    target_date
):

    """往路または復路の日付欄を開いて指定日を選択する。

    引数:
        page: 検索フォーム。field_name: 往路・復路の欄名。target_date: 選択する日付。
    戻り値:
        なし。
    """
    print()
    print(
        f"{field_name}："
        f"{target_date:%Y-%m-%d} を選択"
    )

    field = page.get_by_role(
        "textbox",
        name=field_name,
        exact=True
    )

    await field.click(
        timeout=15000
    )

    section = await navigate_calendar(
        page,
        target_date
    )

    weekdays = [
        "月", "火", "水", "木",
        "金", "土", "日"
    ]

    weekday = weekdays[
        target_date.weekday()
    ]

    link_name = (
        f"{weekday} {target_date.day}"
    )

    day_link = section.get_by_role(
        "link",
        name=link_name,
        exact=True
    )

    if await day_link.count() != 1:

        raise RuntimeError(
            f"日付を特定できません："
            f"{target_date:%Y-%m-%d}"
        )

    await day_link.click(
        timeout=15000
    )

    # 入力された日付を確認
    actual = await field.input_value()

    normalized = re.sub(
        r"\s+",
        "",
        actual
    )

    expected = (
        rf"{target_date.year}年"
        rf"0?{target_date.month}月"
        rf"0?{target_date.day}日"
    )

    if not re.search(
        expected,
        normalized
    ):

        raise RuntimeError(
            f"日付が一致しません：{actual}"
        )

    print(
        f"設定成功：{field_name} = {actual}"
    )


# ==========================================
# 往路・復路・前後3日を設定
# ==========================================

async def set_trip_dates(
    page,
    outbound
):

    """往路出発日と所定日数後の復路出発日を検索フォームに設定する。

    引数:
        page: 検索フォーム。outbound: 往路出発日。
    戻り値:
        設定した復路出発日。
    """
    inbound = outbound + timedelta(
        days=STAY_DAYS
    )

    await select_date(
        page,
        "往路出発日",
        outbound
    )

    await select_date(
        page,
        "復路出発日",
        inbound
    )

    checkbox = page.get_by_role(
        "checkbox",
        name="前後3日の空席を比較"
    )

    await checkbox.check(
        timeout=15000
    )

    if not await checkbox.is_checked():

        raise RuntimeError(
            "前後3日のチェックに失敗しました。"
        )

    print()
    print(
        f"検索条件確定："
        f"{outbound} ～ {inbound}"
    )

    print("前後3日の空席比較：ON")

    return inbound


# ==========================================
# エラーメッセージを検出
# ==========================================

async def is_no_match_error(page):

    # エラーコードの表示を優先して確認
    """指定条件に合う旅程がないことを示す既知のANAエラーを検出する。

    引数:
        page: 結果ページ。
    戻り値:
        エラーが表示されていればTrue。
    """
    error_locator = page.get_by_text(
        re.compile(
            re.escape(NO_MATCH_CODE)
        )
    )

    try:

        count = await error_locator.count()

        for i in range(count):

            if await error_locator.nth(i).is_visible():

                return True

    except PlaywrightError:

        # ページ更新中の一時的なエラーは
        # 次の確認で再試行する
        return False

    return False


# ==========================================
# 検索結果を待つ
# ==========================================

async def wait_for_search_outcome(page):

    """検索後にカレンダーやエラーなどの結果が表示されるまで待機する。

    引数:
        page: 検索結果ページ。
    戻り値:
        判定された結果の種類。
    """
    print()
    print("検索結果を確認しています。")

    # 前回の問題：
    # 往路出発日欄が一時的に消えただけで
    # 別画面へ遷移したと誤判定していた。
    #
    # 今回はその判定を使用しない。

    for second in range(RESULT_TIMEOUT):

        if page.is_closed():

            raise RuntimeError(
                "検索中にブラウザが閉じられました。"
            )

        if await is_no_match_error(page):

            print()
            print(
                f"検出成功：{NO_MATCH_CODE}"
            )

            print(
                "指定条件に一致する結果が"
                "ありませんでした。"
            )

            return "no_match"

        # 5秒ごとに進捗表示
        if second % 5 == 0:

            print(
                f"結果確認中：{second}秒経過"
            )

        await asyncio.sleep(1)

    # エラーが見つからない場合も
    # 空席ありとは判断しない
    print()
    print(
        "指定のエラーコードは"
        "検出されませんでした。"
    )

    print(
        "検索結果画面が表示されている"
        "可能性があります。"
    )

    return "unconfirmed"


# ==========================================
# エラーダイアログを閉じる
# ==========================================

async def close_no_match_dialog(page):

    """該当旅程なしのエラーダイアログを閉じる。

    引数:
        page: エラーが表示されたページ。
    戻り値:
        ダイアログを閉じられたかどうかを示す値。
    """
    print()
    print(
        "エラーダイアログを閉じます。"
    )

    button = page.get_by_role(
        "button",
        name="閉じる",
        exact=True
    )

    if await click_visible(
        button,
        "閉じるボタン"
    ):

        await wait_until_dialog_closed(page)

        return

    # button要素でなければ
    # テキストから探す
    text_button = page.get_by_text(
        "閉じる",
        exact=True
    )

    if await click_visible(
        text_button,
        "閉じる"
    ):

        await wait_until_dialog_closed(page)

        return

    raise RuntimeError(
        "『閉じる』ボタンが見つかりません。"
    )


# ==========================================
# ダイアログが閉じるまで待つ
# ==========================================

async def wait_until_dialog_closed(page):

    """エラーダイアログが消えるまで一定回数確認する。

    引数:
        page: エラーが表示されたページ。
    戻り値:
        ダイアログが閉じたことを示す値。
    """
    for _ in range(10):

        if not await is_no_match_error(page):

            print(
                "エラーダイアログを閉じました。"
            )

            return

        await asyncio.sleep(1)

    raise RuntimeError(
        "エラーダイアログが"
        "閉じられませんでした。"
    )


# ==========================================
# 1回分の検索
# ==========================================

async def search_once(
    page,
    outbound
):

    """指定した日付で検索フォームからANA特典航空券検索を1回行う。

    引数:
        page: 検索フォーム。outbound: 往路出発日。
    戻り値:
        検索結果の判定情報。
    """
    inbound = await set_trip_dates(
        page,
        outbound
    )

    print()
    print(
        f"検索実行：往路 {outbound}"
        f"／ 復路 {inbound}"
    )

    search_button = page.get_by_role(
        "button",
        name="検索する",
        exact=True
    )

    success = await click_visible(
        search_button,
        "検索する"
    )

    if not success:

        raise RuntimeError(
            "検索ボタンが見つかりません。"
        )

    # 検索処理の開始を待つ
    await page.wait_for_timeout(1500)

    outcome = await wait_for_search_outcome(
        page
    )

    return outcome


# ==========================================
# ANA予約ページへ移動
# ==========================================

async def open_booking_page(
    context,
    page
):

    """ANAのトップページから特典航空券予約画面を開く。

    引数:
        context: ブラウザコンテキスト。page: ANAトップページ。
    戻り値:
        予約画面のページオブジェクト。
    """
    reservation = page.get_by_role(
        "link",
        name=re.compile("特典航空券予約")
    )

    try:

        async with page.expect_popup(
            timeout=15000
        ) as popup_info:

            await reservation.first.click(
                timeout=15000
            )

        booking_page = await popup_info.value

        await booking_page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )

        print("予約ページを開きました。")

        return booking_page

    except PlaywrightError as error:

        print(
            "予約ページへの自動移動に失敗しました。"
        )

        print(str(error)[:200])

        await asyncio.to_thread(
            input,
            "手動で予約ページを開いてEnter："
        )

        tabs = [
            tab for tab in context.pages
            if tab != page
            and not tab.is_closed()
        ]

        return tabs[-1] if tabs else page


# ==========================================
# ANAログイン
# ==========================================

async def login_ana(page, password):

    """ログイン状態を確認し、必要に応じてANAのパスワードを入力する。

    引数:
        page: ログイン画面。password: 環境変数から取得したパスワード。
    戻り値:
        なし。
    """
    print()
    print("ログイン状態を確認します。")

    password_field = page.locator(
        'input[type="password"]:visible'
    )

    try:

        await password_field.first.wait_for(
            state="visible",
            timeout=10000
        )

        if await password_field.count() == 1:

            await password_field.first.fill(
                password,
                timeout=15000
            )

            print(
                "パスワードを自動入力しました。"
            )

            login_button = page.get_by_role(
                "button",
                name="ログイン",
                exact=True
            )

            if await login_button.count() == 1:

                await login_button.click(
                    timeout=15000
                )

                print(
                    "ログインを実行しました。"
                )

    except PlaywrightTimeoutError:

        print(
            "パスワード入力欄がありません。"
        )

        print(
            "ログイン済みの可能性があります。"
        )


# ==========================================
# 検索フォームが表示されるまで待つ
# ==========================================

async def wait_for_search_form(page):

    """ログイン後、特典航空券の検索フォームが操作可能になるまで待つ。

    引数:
        page: 予約画面。
    戻り値:
        フォーム確認後の処理結果。
    """
    departure_field = page.get_by_role(
        "textbox",
        name="出発地",
        exact=True
    )

    try:

        await departure_field.wait_for(
            state="visible",
            timeout=20000
        )

    except PlaywrightTimeoutError:

        print(
            "検索画面を確認してください。"
        )

        await asyncio.to_thread(
            input,
            "検索画面が表示されたらEnter："
        )

        await departure_field.wait_for(
            state="visible",
            timeout=15000
        )

    print(
        "特典航空券検索画面を確認しました。"
    )


# ==========================================
# メイン処理
# ==========================================

async def main():

    """ブラウザ起動・ログイン・検索などを順番に実行する単独テスト用の入口。

    引数:
        なし。
    戻り値:
        なし。
    """
    password = os.getenv(
        "ANA_PASSWORD"
    )

    if not password:

        print(
            ".env にANA_PASSWORDが"
            "設定されていません。"
        )

        return

    start_date = add_months(
        date.today(),
        2
    )

    print()
    print(
        "===== ANA特典航空券自動検索 ====="
    )

    print(
        f"最初の往路：{start_date}"
    )

    print(
        f"滞在：{STAY_DAYS}泊"
    )

    print(
        f"検索間隔："
        f"{SEARCH_INTERVAL_DAYS}日"
    )

    print(
        f"最大検索回数：{MAX_SEARCHES}回"
    )

    async with async_playwright() as p:

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={
                "width": 1280,
                "height": 900
            }
        )

        try:

            # ==================================
            # STEP 1：ANA公式サイト
            # ==================================

            page = (
                context.pages[0]
                if context.pages
                else await context.new_page()
            )

            print()
            print("ANAサイトを開きます。")

            await page.goto(
                ANA_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print("ANAサイトを開きました。")

            # ==================================
            # STEP 2：海外タブ
            # ==================================

            overseas = page.get_by_role(
                "tab",
                name=re.compile("海外")
            )

            if await overseas.count() > 0:

                await click_visible(
                    overseas,
                    "海外タブ"
                )

            # ==================================
            # STEP 3：特典航空券タブ
            # ==================================

            award_tab = page.get_by_role(
                "tab",
                name="特典航空券",
                exact=True
            )

            success = await click_visible(
                award_tab,
                "特典航空券タブ"
            )

            if not success:

                await asyncio.to_thread(
                    input,
                    "特典航空券タブを選択してEnter："
                )

            # ==================================
            # STEP 4：予約ページ
            # ==================================

            booking_page = await open_booking_page(
                context,
                page
            )

            # ==================================
            # STEP 5：ログイン
            # ==================================

            await login_ana(
                booking_page,
                password
            )

            # ==================================
            # STEP 6：検索画面
            # ==================================

            await wait_for_search_form(
                booking_page
            )

            # ==================================
            # STEP 7：基本検索条件
            # ==================================

            await fill_search_form(
                booking_page
            )

            # ==================================
            # STEP 8：繰り返し検索
            # ==================================

            for index in range(MAX_SEARCHES):

                outbound = (
                    start_date
                    + timedelta(
                        days=(
                            SEARCH_INTERVAL_DAYS
                            * index
                        )
                    )
                )

                print()
                print(
                    "=============================="
                )

                print(
                    f"検索 {index + 1}/{MAX_SEARCHES} 回目"
                )

                print(
                    "=============================="
                )

                outcome = await search_once(
                    booking_page,
                    outbound
                )

                # --------------------------
                # 指定条件に一致する結果なし
                # --------------------------

                if outcome == "no_match":

                    print()
                    print(
                        "検索条件に一致する結果なし。"
                    )

                    await close_no_match_dialog(
                        booking_page
                    )

                    if index == MAX_SEARCHES - 1:

                        print(
                            "最大検索回数に達しました。"
                        )

                        break

                    next_outbound = (
                        outbound
                        + timedelta(
                            days=SEARCH_INTERVAL_DAYS
                        )
                    )

                    next_inbound = (
                        next_outbound
                        + timedelta(
                            days=STAY_DAYS
                        )
                    )

                    print()
                    print(
                        "次の検索条件："
                    )

                    print(
                        f"往路：{next_outbound}"
                    )

                    print(
                        f"復路：{next_inbound}"
                    )

                    print(
                        f"{SEARCH_DELAY}秒後に"
                        "再検索します。"
                    )

                    await asyncio.sleep(
                        SEARCH_DELAY
                    )

                    continue

                # --------------------------
                # エラー未検出
                # --------------------------

                if outcome == "unconfirmed":

                    print()
                    print(
                        "検索結果を判定できません。"
                    )

                    print(
                        "空席が表示されている"
                        "可能性もあるため、"
                        "自動検索を停止します。"
                    )

                    break

            # ==================================
            # STEP 9：実行終了
            # ==================================

            print()
            print(
                "===== 検索処理終了 ====="
            )

            await asyncio.to_thread(
                input,
                "ブラウザを確認後、Enterで終了："
            )

        except (
            PlaywrightError,
            RuntimeError
        ) as error:

            print()
            print(
                "処理を停止しました。"
            )

            print(str(error))

            await asyncio.to_thread(
                input,
                "画面を確認後、Enterで終了："
            )

        finally:

            await context.close()

    print(
        "プログラムを終了しました。"
    )


if __name__ == "__main__":
    asyncio.run(main())
