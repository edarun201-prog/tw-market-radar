"""AI 台股市場雷達：資料管線（抓取 → 正規化 → 驗證 → 入庫）。"""
import os
import sys


def _add_windows_libpq() -> None:
    """Windows 用一般版 psycopg（智慧型應用程式控制會擋 psycopg-binary 沒有簽章的 DLL），改用 PostgreSQL 附帶、
    有簽章的 libpq.dll。工作排程器用 pythonw 執行時不一定有使用者的 PATH，所以由程式自己找：
    先看 PG_BIN，再找 Program Files\\PostgreSQL 底下版本最新的 bin。只加在這個程序裡，不改系統設定。"""
    candidates = [os.environ["PG_BIN"]] if os.environ.get("PG_BIN") else []
    base = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "PostgreSQL")
    if os.path.isdir(base):
        versions = sorted((d for d in os.listdir(base) if d.isdigit()), key=int, reverse=True)
        candidates += [os.path.join(base, v, "bin") for v in versions]
    for bin_dir in candidates:
        if os.path.exists(os.path.join(bin_dir, "libpq.dll")):
            os.add_dll_directory(bin_dir)  # libpq 依賴的 libssl 等 DLL 在同一個資料夾
            # psycopg 用 ctypes.util.find_library 找 libpq，Windows 上只看 PATH；放在最後，PATH 原本有的優先
            os.environ["PATH"] = os.environ.get("PATH", "").rstrip(os.pathsep) + os.pathsep + bin_dir
            return


if sys.platform == "win32":
    _add_windows_libpq()
