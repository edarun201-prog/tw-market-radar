"""命令列入口：python -m radar <指令>

  probe      抓一天資料，只印出表格標題與欄位（不寫資料庫），用來確認官方格式
  ingest     抓指定日期並入庫
  prefetch   只下載一段期間的原始檔到 data/raw（不需資料庫，可中斷後續跑）
  backfill   回補一段期間（可中斷後續跑；--from-raw 用已下載的原始檔，不發請求）
  reprocess  用已存的原始檔重新正規化與入庫（不發請求）
  status     查看最近的抓取紀錄
  scheduler  啟動每日排程（Docker worker 預設指令）
  ex-rights  除權息（TWT49U），以月為單位回補
  companies  上市公司基本資料與產業別
  analyze    計算特徵（還原價）與異常訊號，印出每天訊號數
  pages      公開網頁：把最新的 HTML 檔推到 GitHub Pages（.env 設定 PAGES_REPO 才會做；每日流程也會做）
  tpex       上櫃（櫃買中心 OpenAPI）：抓最新一天的行情、三大法人、除權息（每日流程也會做）
  outcomes   訊號回測：重算每一筆訊號之後的報酬（analyze 也會順便做）
  summary    盤後摘要（有 Claude API 金鑰用 AI，沒有就用模擬摘要）
  export     把某一天的網站存成一個 HTML 檔
  audit      一致性檢查（除權息 vs 生效日成交價）
  web        啟動網站與 JSON API（預設 http://127.0.0.1:8000）
  daily      跑一次每日流程就結束（Windows 工作排程器用）
  migrate    不用 dbmate 建立資料表（本機 PostgreSQL 用）
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from radar import repository as repo
from radar.adapters.twse import TwseAdapter
from radar.config import TAIPEI, load_settings
from radar.jobs.backfill import backfill
from radar.jobs.ingest import ingest_date
from radar.normalizers.common import iter_tables


def _date(s: str) -> date:
    return date.fromisoformat(s)


FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _probe_fetchers(adapter, d: date, dataset: str | None):
    if dataset == "TWT49U":
        from radar.jobs.corporate import month_ranges
        start, end = month_ranges(d.replace(day=1), d.replace(day=1) + timedelta(days=31))[0]
        return [(lambda: adapter.fetch_ex_rights(start, end), f"twse_twt49u_{start:%Y%m}.json")]
    if dataset == "COMPANY":
        return [(lambda: adapter.fetch_company_profiles(d), f"twse_company_{d:%Y%m%d}.json")]
    return [(lambda: adapter.fetch_market_day(d), None), (lambda: adapter.fetch_institutional(d), None)]


_COMPANY_FIXTURE_KEYS = ("出表日期", "公司代號", "公司名稱", "公司簡稱", "產業別", "上市日期")


def _fixture_body(dataset: str, body: dict) -> dict:
    """公司資料含董事長、電話、Email 等個人資訊，測試資料只留解析會用到的欄位（原始檔仍完整保存）。"""
    if dataset != "COMPANY":
        return body
    return {"data": [{k: r.get(k) for k in _COMPANY_FIXTURE_KEYS} for r in body["data"]],
            "industry_names": [{"公司代號": r.get("公司代號"), "產業別": r.get("產業別")} for r in body["industry_names"]]}


def cmd_probe(args, settings) -> None:
    adapter = TwseAdapter(settings)
    for fetch, fixture_name in _probe_fetchers(adapter, args.date, args.dataset):
        p = fetch()
        print(f"\n=== {p.dataset} {p.trade_date} stat={p.meta.get('stat')} has_data={p.has_data}")
        print(f"URL: {p.url}")
        print(f"body keys: {list(p.body)}")
        tables = iter_tables(p.body)
        if p.body.get("fields"):
            print(f"[top-level] fields={p.body['fields']}")
        if isinstance(p.body.get("data"), list):
            print(f"[top-level] data {len(p.body['data'])} 列，第一列：{(p.body['data'] or [None])[0]}")
        for t in tables:
            print(f"[{t.title}] {len(t.data)} 列")
            print(f"  fields={t.fields}")
            if t.data:
                print(f"  第一列：{t.data[0]}")
        if args.save_fixture and fixture_name:
            path = FIXTURES / fixture_name
            path.write_text(json.dumps(_fixture_body(p.dataset, p.body), ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"已存成測試資料：{path}")


def cmd_ingest(args, settings) -> None:
    with repo.connect(settings.database_url) as conn:
        r = ingest_date(conn, TwseAdapter(settings), settings, args.date, final=not args.not_final)
    print(r.status)


def _range(args) -> tuple[date, date]:
    end = args.end or datetime.now(TAIPEI).date() - timedelta(days=1)
    return args.start or end - timedelta(days=365), end


def cmd_prefetch(args, settings) -> None:
    from radar.jobs.prefetch import prefetch
    start, end = _range(args)
    print(dict(prefetch(TwseAdapter(settings), settings, start, end, force=args.force)))


def cmd_backfill(args, settings) -> None:
    start, end = _range(args)
    with repo.connect(settings.database_url) as conn:
        totals = backfill(conn, TwseAdapter(settings), settings, start, end, force=args.force,
                          from_raw=args.from_raw)
    print(dict(totals))


def cmd_reprocess(args, settings) -> None:
    with repo.connect(settings.database_url) as conn:
        r = ingest_date(conn, TwseAdapter(settings), settings, args.date, final=True, from_raw=True)
    print(r.status)


def cmd_status(args, settings) -> None:
    with repo.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT r.target_date, r.dataset, r.status, r.row_count, r.warning_count, r.attempts,
                   left(coalesce(r.error, ''), 80)
              FROM ingestion_runs r
             WHERE r.target_date >= current_date - %s::int
             ORDER BY r.target_date DESC, r.dataset
            """,
            (args.days,),
        ).fetchall()
        summary = conn.execute(
            """
            SELECT dataset, status, count(*) FROM ingestion_runs GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
    print("日期        資料集    狀態      筆數   警告  次數  錯誤")
    for d, ds, st, n, w, a, err in rows:
        print(f"{d}  {ds:<8}  {st:<8}  {n or '-':>5}  {w or 0:>4}  {a:>4}  {err}")
    print("\n全部紀錄統計：", {f"{ds}:{st}": n for ds, st, n in summary})


def cmd_scheduler(args, settings) -> None:
    from radar.jobs.scheduler import start
    start(settings, catch_up_on_start=not args.no_catch_up)


def cmd_ex_rights(args, settings) -> None:
    from radar.jobs.corporate import backfill_ex_rights
    today = datetime.now(TAIPEI).date()
    end = args.end or today
    start = args.start or (end - timedelta(days=365)).replace(day=1)
    with repo.connect(settings.database_url) as conn:
        totals = backfill_ex_rights(conn, TwseAdapter(settings), settings, start, end, today,
                                    force=args.force, from_raw=args.from_raw)
    print(dict(totals))


def cmd_companies(args, settings) -> None:
    from radar.jobs.corporate import sync_companies
    as_of = args.date or datetime.now(TAIPEI).date()
    with repo.connect(settings.database_url) as conn:
        print(sync_companies(conn, TwseAdapter(settings), settings, as_of, from_raw=args.from_raw))


def cmd_analyze(args, settings) -> None:
    from radar.features import build_features
    from radar.signals import TYPES, daily_counts_report, generate_signals
    with repo.connect(settings.database_url) as conn:
        first, last = conn.execute("SELECT min(trade_date), max(trade_date) FROM daily_prices").fetchone()
        start, end = args.start or first, args.end or last
        if not args.report_only:
            print(f"特徵 {start}～{end}：{build_features(conn, start, end)} 列")
            print(f"訊號：{dict(generate_signals(conn, start, end))}")
            from radar.outcomes import build_outcomes
            print(f"回測：{build_outcomes(conn)}")
        print(f"\n每天訊號數（{start}～{end}）")
        print(f"{'類型':<12}{'中位數':>6}{'P90':>6}{'最多':>6}{'總數':>7}")
        for typ, med, p90, mx, tot in daily_counts_report(conn, start, end):
            print(f"{TYPES.get(typ, typ):<12}{med:>6.0f}{p90:>6.0f}{mx:>6}{tot:>7}")


def cmd_pages(args, settings) -> None:
    from radar.jobs.scheduler import export_latest, publish_latest_pages
    if not settings.pages_repo:
        print("沒有設定 PAGES_REPO（.env），不發布。")
        return
    export_latest(settings)
    publish_latest_pages(settings, force=args.force)


def cmd_tpex(args, settings) -> None:
    from radar.jobs.scheduler import update_tpex
    update_tpex(settings)


def cmd_outcomes(args, settings) -> None:
    from radar.outcomes import build_outcomes
    with repo.connect(settings.database_url) as conn:
        print(f"回測：{build_outcomes(conn)}")


def cmd_summary(args, settings) -> None:
    from radar import summary
    with repo.connect(settings.database_url) as conn:
        latest = conn.execute("SELECT max(trade_date) FROM market_breadth").fetchone()[0]
        d = args.date or latest
        if args.show_facts:
            print(json.dumps(summary.build_facts(conn, d), ensure_ascii=False, indent=1))
            return
        use_ai = summary.has_credentials() and not args.simulate
        if args.all:
            days = [r[0] for r in conn.execute("SELECT trade_date FROM market_breadth ORDER BY 1").fetchall()]
            done = sum(1 for x in days if (summary.generate_summary(conn, x, force=args.force) if use_ai
                                           else summary.simulate(conn, x, force=args.force)))
            print(f"{'AI' if use_ai else '模擬'}摘要：{done}／{len(days)} 天")
            return
        if not use_ai and not args.simulate:
            print("沒有設定 Claude API 金鑰（.env 的 ANTHROPIC_API_KEY 或 ant auth login），改用模擬摘要。")
        text = summary.generate_summary(conn, d, force=args.force) if use_ai else summary.simulate(conn, d, force=args.force)
    print(text if text else "沒有產生摘要（被拒絕或沒通過檢查，詳見 log）。")


def cmd_export(args, settings) -> None:
    from radar.web.export import export_day
    with repo.connect(settings.web_database_url) as conn:
        d = args.date or conn.execute("SELECT max(trade_date) FROM market_breadth").fetchone()[0]
        path = export_day(conn, d, args.out or settings.export_dir)
    print(f"已匯出：{path}")


def cmd_audit(args, settings) -> None:
    from radar.audit import check_adjusted_returns, check_corporate_actions
    with repo.connect(settings.database_url) as conn:
        bad = check_corporate_actions(conn)
        jumps = check_adjusted_returns(conn)
        n = conn.execute("SELECT count(*) FROM corporate_actions").fetchone()[0]
    print(f"還原後單日報酬超過漲跌幅：{len(jumps)} 筆")
    for j in jumps:
        print(f"  {j['symbol']} {j['name']} {j['trade_date']} 還原日報酬 {float(j['ret_1d']):+.2%}")
    print(f"除權息 {n} 筆，與生效日成交價矛盾 {len(bad)} 筆")
    for b in bad:
        src = "官方漲跌停" if b["official"] else "推算漲跌停"
        print(f"  {b['symbol']} {b['name']} 除權息 {b['ex_date']}（生效 {b['eff_date']}）{b['problem']}："
              f"最高 {b['high']}／最低 {b['low']}，{src} {b['lower']:.2f}～{b['upper']:.2f}")


def cmd_web(args, settings) -> None:
    import uvicorn
    from radar import live
    from radar.web.app import create_app
    print(f"網站：http://{args.host}:{args.port}/（API 文件：/api/docs）")
    provider = live.make_provider(settings.live_provider)
    if provider and not provider.public_ok and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"注意：盤中即時行情目前用「{provider.name}」，只適合自己看。"
              "要讓別人看，請先取得授權並換成有授權的資料來源，或在 .env 設定 LIVE_PROVIDER=off。")
    # 有 --log-file（例：開機自動啟動、用 pythonw 沒有主控台）時，網站的紀錄也寫進同一個檔案
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning",
                **({"log_config": None} if getattr(args, "log_file", None) else {}))


def cmd_daily(args, settings) -> None:
    from radar.jobs.scheduler import run_daily
    run_daily(settings)


def cmd_migrate(args, settings) -> None:
    from radar.migrate import migrate
    with repo.connect(settings.database_url) as conn:
        applied = migrate(conn)
    print("已執行：" + "、".join(applied) if applied else "資料表已是最新")


def main() -> None:
    settings = load_settings()
    ap = argparse.ArgumentParser(prog="python -m radar")
    ap.add_argument("--log-file", type=Path,
                    help="紀錄寫到這個檔案（UTF-8，附加）；工作排程器用 pythonw 執行時沒有主控台，一定要指定")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe"); p.add_argument("--date", type=_date, required=True)
    p.add_argument("--dataset", choices=["TWT49U", "COMPANY"],
                   help="預設印出 MI_INDEX 與 T86；TWT49U 查 date 所在月份，COMPANY 查當下快照")
    p.add_argument("--save-fixture", action="store_true", help="把回應存到 tests/fixtures（TWT49U、COMPANY 用）")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("ingest"); p.add_argument("--date", type=_date, required=True)
    p.add_argument("--not-final", action="store_true", help="查無資料時視為尚未公布，而不是休市")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("prefetch")
    p.add_argument("--start", type=_date, help="預設為 end 往前 365 天")
    p.add_argument("--end", type=_date, help="預設為昨天")
    p.add_argument("--force", action="store_true", help="已存在的原始檔也重新下載")
    p.set_defaults(fn=cmd_prefetch)

    p = sub.add_parser("backfill")
    p.add_argument("--start", type=_date, help="預設為 end 往前 365 天")
    p.add_argument("--end", type=_date, help="預設為昨天")
    p.add_argument("--force", action="store_true", help="已成功的日期也重抓")
    p.add_argument("--from-raw", action="store_true", help="用 data/raw 的原始檔入庫，不發請求")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("reprocess"); p.add_argument("--date", type=_date, required=True)
    p.set_defaults(fn=cmd_reprocess)

    p = sub.add_parser("status"); p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("scheduler"); p.add_argument("--no-catch-up", action="store_true")
    p.set_defaults(fn=cmd_scheduler)

    p = sub.add_parser("ex-rights", help="除權息，以月為單位回補；已過完的月份成功後不再重抓")
    p.add_argument("--start", type=_date, help="預設為一年前的月初")
    p.add_argument("--end", type=_date, help="預設為今天")
    p.add_argument("--force", action="store_true")
    p.add_argument("--from-raw", action="store_true")
    p.set_defaults(fn=cmd_ex_rights)

    p = sub.add_parser("companies", help="更新上市公司基本資料與產業別（建議每週一次）")
    p.add_argument("--date", type=_date, help="預設為今天；搭配 --from-raw 重算某天的原始檔")
    p.add_argument("--from-raw", action="store_true")
    p.set_defaults(fn=cmd_companies)

    p = sub.add_parser("analyze", help="計算特徵與訊號（預設全部期間），並印出每天訊號數")
    p.add_argument("--start", type=_date)
    p.add_argument("--end", type=_date)
    p.add_argument("--report-only", action="store_true", help="只印報表，不重算")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("pages", help="公開網頁：把最新的單一 HTML 檔發布到 GitHub Pages（gh-pages 分支）")
    p.add_argument("--force", action="store_true", help="內容沒變也重新推送")
    p.set_defaults(fn=cmd_pages)

    p = sub.add_parser("tpex", help="上櫃（櫃買中心 OpenAPI）：抓它目前提供的最新一天")
    p.set_defaults(fn=cmd_tpex)

    p = sub.add_parser("outcomes", help="訊號回測：重算每一筆訊號之後的報酬")
    p.set_defaults(fn=cmd_outcomes)

    p = sub.add_parser("summary", help="產生 AI 盤後摘要（需要 Claude API 金鑰）")
    p.add_argument("--date", type=_date, help="預設為最新交易日")
    p.add_argument("--force", action="store_true", help="已經有摘要也重寫")
    p.add_argument("--show-facts", action="store_true", help="只印出要給模型的 facts，不呼叫 API")
    p.add_argument("--simulate", action="store_true", help="用模擬摘要（依規則組句），不呼叫 API")
    p.add_argument("--all", action="store_true", help="替所有交易日產生摘要（沒有的才產生；--force 重寫）")
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("export", help="把某一天的網站存成一個 HTML 檔（不需要伺服器、可以直接傳給別人）")
    p.add_argument("--date", type=_date, help="預設為最新交易日")
    p.add_argument("--out", type=Path, help="輸出資料夾，預設為 data/exports")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("audit", help="一致性檢查：除權息紀錄與生效日成交價（漲跌停範圍）是否矛盾")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("web", help="啟動網站（預設只接受本機連線）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("daily", help="補抓漏掉的日子，到了排程時間再抓今天；跑完就結束")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("migrate", help="不用 dbmate 建立或更新資料表")
    p.set_defaults(fn=cmd_migrate)

    args = ap.parse_args()
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt, filename=args.log_file, encoding="utf-8")
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 每個請求一行太吵；失敗與重試仍由 adapter 記錄
    try:
        args.fn(args, settings)
    except Exception:
        logging.getLogger("radar").exception("指令 %s 失敗", args.cmd)   # 沒有主控台時錯誤也要留在紀錄檔
        raise SystemExit(1)


if __name__ == "__main__":
    main()
