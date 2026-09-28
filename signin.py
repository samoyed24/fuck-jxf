#!/usr/bin/env python3
"""
fuck-jxf — 自动登录 + 验证码识别 + 签到

流程：
  1. GET  /server/captchaImage   取验证码（base64 图 + uuid）
  2. OCR  识别算式并计算          （ddddocr，实测 20/20）
  3. POST /server/login          账号密码 + 验证码 → JWT
  4. GET  /server/stu/in/list    查记录 / 回溯上次成功签到的坐标
  5. POST /server/stu/in         提交签到（JSON）

凭据来源：
  本地：.env 文件（见 .env.example）
  CI ：GitHub Secrets 注入的环境变量

两者变量名一致，读取路径完全相同 —— 本地跑通即 CI 跑通。

坐标策略（重要）：
  后端按位置偏差判定异常签到（isWarning），因此坐标不提供手动配置，
  只复用「最近一次成功签到」的坐标，保证每次完全一致。
  若近 30 天内无签到记录，脚本报错退出，需先手动签到一次。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import ddddocr
import requests
from dotenv import load_dotenv

BASE = "https://jlujxf.com/server"
HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
LOG = HERE / "signin.log"

# 加载 .env（本地）；CI 里文件不存在，直接用已注入的环境变量
load_dotenv(ENV_FILE)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) "
    "UnifiedPCMacWechat(0xf2641b50) XWEB/20008"
)
REFERER = "https://servicewechat.com/wxb2776d32c71fea79/9/page-frame.html"

# 坐标回溯天数上限
LOOKBACK_DAYS = 30

_ocr: ddddocr.DdddOcr | None = None


def ocr() -> ddddocr.DdddOcr:
    global _ocr
    if _ocr is None:
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:                                   # CI 里工作目录可能只读
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def headers(token: str | None = None, json_body: bool = False) -> dict[str, str]:
    h = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Content-Type": "application/json" if json_body
                       else "application/x-www-form-urlencoded",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    """从环境变量读取配置（.env 已由 load_dotenv 注入）。"""
    cfg: dict = {}

    for env_key, cfg_key in (
        ("JXF_USERNAME", "username"),
        ("JXF_PASSWORD", "password"),
    ):
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val

    cfg.setdefault("captcha_retries", 6)
    cfg.setdefault("request_timeout", 15)

    if not cfg.get("username") or not cfg.get("password"):
        raise SystemExit(
            "缺少凭据：请复制 .env.example 为 .env 并填写，"
            "或设置环境变量 JXF_USERNAME / JXF_PASSWORD"
        )
    return cfg


# ---------------------------------------------------------------- 验证码

def solve_captcha(expr: str) -> int | None:
    """提取算式并计算。OCR 常把 '?' 读成 '>'/'7'，故只取前三个 token。"""
    t = expr.replace("×", "*").replace("x", "*").replace("X", "*").replace("÷", "/")
    m = re.search(r"(\d+)\s*([+\-*/])\s*(\d+)", t)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    return a // b if b else None


def get_captcha(sess: requests.Session, timeout: int) -> tuple[int, str] | None:
    try:
        r = sess.get(f"{BASE}/captchaImage", headers=headers(), timeout=timeout)
        d = r.json()
    except Exception as e:
        log(f"  取验证码失败: {e}")
        return None

    if not d.get("captchaEnabled"):
        return 0, ""                       # 后端关掉了验证码
    raw = base64.b64decode(d["img"])
    text = ocr().classification(raw)
    val = solve_captcha(text)
    if val is None:
        log(f"  OCR 无法解析: {text!r}")
        return None
    log(f"  验证码 OCR={text!r} → {val}")
    return val, d["uuid"]


# ---------------------------------------------------------------- 登录

def login(sess: requests.Session, cfg: dict) -> str | None:
    timeout = cfg["request_timeout"]
    for attempt in range(1, cfg["captcha_retries"] + 1):
        cap = get_captcha(sess, timeout)
        if cap is None:
            continue
        code, uuid = cap
        try:
            r = sess.post(
                f"{BASE}/login",
                headers=headers(),
                data={
                    "username": cfg["username"],
                    "password": cfg["password"],
                    "code": str(code),
                    "uuid": uuid,
                    "wxcode": "",
                },
                timeout=timeout,
            )
            d = r.json()
        except Exception as e:
            log(f"  登录请求异常: {e}")
            continue

        if d.get("code") == 200 and d.get("token"):
            log(f"登录成功（第 {attempt} 次尝试）")
            return d["token"]
        log(f"  登录失败: code={d.get('code')} msg={d.get('msg')}")
    log("登录失败：重试用尽")
    return None


# ---------------------------------------------------------------- 签到

def fetch_records(sess: requests.Session, cfg: dict, token: str,
                  begin: str, end: str) -> list[dict]:
    """查 [begin, end] 区间内的签到记录，按时间升序返回。"""
    try:
        r = sess.get(
            f"{BASE}/stu/in/list",
            headers=headers(token),
            params={"pageNum": 1, "pageSize": 100,
                    "beginCreatedAt": begin, "endCreatedAt": end},
            timeout=cfg["request_timeout"],
        )
        rows = r.json().get("rows") or []
        return sorted(rows, key=lambda x: x.get("createdAt") or "")
    except Exception as e:
        log(f"  查询 {begin}~{end} 记录失败: {e}")
        return []


def resolve_coords(sess: requests.Session, cfg: dict,
                   token: str) -> tuple[float, float]:
    """
    取最近一次成功签到的坐标。

    坐标不提供手动配置 —— 后端按位置偏差判定异常签到，手填的坐标一旦与
    历史记录不符就会被标记。因此只复用历史值，保证每次完全一致。

    区间查询而非只看今天 —— CI 在清晨跑，当天通常还没有记录。
    """
    begin = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = date.today().isoformat()
    recs = fetch_records(sess, cfg, token, begin, end)

    if recs:
        last = recs[-1]
        log(f"复用 {last.get('createdAt')} 的坐标 "
            f"({last['lng']}, {last['lat']})")
        return float(last["lng"]), float(last["lat"])

    raise SystemExit(
        f"近 {LOOKBACK_DAYS} 天内没有签到记录，无法取得坐标。\n"
        "  请先在小程序里手动签到一次，之后脚本会自动复用该坐标。"
    )


def do_signin(sess: requests.Session, cfg: dict, token: str,
              lng: float, lat: float) -> bool:
    try:
        r = sess.post(
            f"{BASE}/stu/in",
            headers=headers(token, json_body=True),
            data=json.dumps({"lng": lng, "lat": lat}),
            timeout=cfg["request_timeout"],
        )
        d = r.json()
    except Exception as e:
        log(f"签到请求异常: {e}")
        return False

    if d.get("code") == 200:
        log(f"✓ 签到成功  ({lng}, {lat})")
        return True

    msg = d.get("msg") or ""
    if "已签到" in msg or "重复打卡" in msg:
        log(f"今天已签到过，无需重复（{msg}）")
        return True
    log(f"✗ 签到失败: code={d.get('code')} msg={msg}")
    return False


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="fuck-jxf 自动签到")
    ap.add_argument("--dry-run", action="store_true", help="只登录+查状态，不提交")
    ap.add_argument("--force", action="store_true", help="今天已签到时仍再签一次")
    ap.add_argument("--check", action="store_true", help="只显示状态")
    args = ap.parse_args()

    cfg = load_config()
    log(f"账号 {cfg['username']}")

    sess = requests.Session()
    token = login(sess, cfg)
    if not token:
        return 1

    today = date.today().isoformat()
    recs = fetch_records(sess, cfg, token, today, today)
    log(f"今天已有 {len(recs)} 条签到记录")
    for rec in recs:
        log(f"  id={rec.get('id')} 坐标=({rec.get('lng')}, {rec.get('lat')}) "
            f"地址={rec.get('address')} 时间={rec.get('createdAt')}")

    if args.check:
        return 0

    if recs and not args.force:
        log("今天已签到，跳过（--force 可强制再签）")
        return 0

    lng, lat = resolve_coords(sess, cfg, token)
    if args.dry_run:
        log(f"[dry-run] 不实际提交（坐标 {lng}, {lat}）")
        return 0

    return 0 if do_signin(sess, cfg, token, lng, lat) else 1


if __name__ == "__main__":
    sys.exit(main())
