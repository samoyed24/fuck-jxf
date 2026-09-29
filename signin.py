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

通知通道（可选，可同时启用）：
  1. SMTP 邮件    —— JXF_MAIL_* 系列
  2. Portcloud Notify（HTTP API）—— JXF_PC_* 系列
  何时通知由 JXF_NOTIFY_SUCCESS / JXF_NOTIFY_FAILURE 控制，与通道无关。
  通知发送失败不会影响签到结果。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import ddddocr
import requests
from dotenv import load_dotenv

BASE = "https://jlujxf.com/server"
HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
LOG = HERE / "signin.log"

# 加载 .env（本地）；CI 里文件不存在，直接用已注入的环境变量
load_dotenv(ENV_FILE)

# 签到以北京时间划分自然日。
# 不能用 date.today()：GitHub Actions runner 的本地时区是 UTC，
# 若 cron 落在北京时间 08:00 之前，runner 的"今天"会比用户认知的早一天，
# 导致查询错日期 —— 可能误判「今天已签到」而跳过，或重复签到。
TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    """当前北京时间（时区感知）。"""
    return datetime.now(TZ)


def today_str() -> str:
    """北京时间的今天，YYYY-MM-DD。"""
    return now().date().isoformat()

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
    line = f"[{now():%Y-%m-%d %H:%M:%S}] {msg}"
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

def _parse_bool(raw: str, env_key: str) -> bool:
    """严格解析布尔值。无法识别时显式失败，不静默取默认值。"""
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise SystemExit(
        f"环境变量 {env_key} 的值无法识别：{raw!r}\n"
        "  可选值：true/false、yes/no、1/0、on/off"
    )


def load_config() -> dict:
    """从环境变量读取配置（.env 已由 load_dotenv 注入）。"""
    cfg: dict = {}

    for env_key, cfg_key in (
        ("JXF_USERNAME", "username"),
        ("JXF_PASSWORD", "password"),
        # 邮件通道（SMTP）
        ("JXF_MAIL_HOST", "mail_host"),
        ("JXF_MAIL_PORT", "mail_port"),
        ("JXF_MAIL_USER", "mail_user"),
        ("JXF_MAIL_PASSWORD", "mail_password"),
        ("JXF_MAIL_TO", "mail_to"),
        # Portcloud Notify 通道（HTTP API）
        ("JXF_PC_KEY", "pc_key"),
        ("JXF_PC_TO", "pc_to"),
        ("JXF_PC_URL", "pc_url"),
    ):
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val

    cfg.setdefault("captcha_retries", 6)
    cfg.setdefault("request_timeout", 15)

    # 通知开关：与通道无关，控制「何时通知」。
    # 优先读新的通用名，回退到旧的 JXF_MAIL_* 以兼容既有配置。
    # 默认两个都发：成功通知同时充当心跳信号 —— 若某周未收到，
    # 说明任务没跑（如 workflow 被停用、凭据失效），可据此察觉静默故障。
    raw_success = (os.environ.get("JXF_NOTIFY_SUCCESS")
                   or os.environ.get("JXF_MAIL_NOTIFY_SUCCESS") or "true")
    raw_failure = (os.environ.get("JXF_NOTIFY_FAILURE")
                   or os.environ.get("JXF_MAIL_NOTIFY_FAILURE") or "true")
    cfg["notify_success"] = _parse_bool(raw_success, "JXF_NOTIFY_SUCCESS")
    cfg["notify_failure"] = _parse_bool(raw_failure, "JXF_NOTIFY_FAILURE")

    # 端口
    if cfg.get("mail_port"):
        try:
            cfg["mail_port"] = int(cfg["mail_port"])
        except ValueError:
            raise SystemExit(
                f"环境变量 JXF_MAIL_PORT 必须是数字：{cfg['mail_port']!r}")
    else:
        cfg["mail_port"] = 465

    # --- 通道 1：SMTP 邮件 ---
    # 要么全配，要么全不配
    mail_keys = ("mail_host", "mail_user", "mail_password", "mail_to")
    present = [k for k in mail_keys if cfg.get(k)]
    if present and len(present) != len(mail_keys):
        missing = [k for k in mail_keys if not cfg.get(k)]
        raise SystemExit(
            "邮件配置不完整。缺少：" + "、".join(missing) + "\n"
            "  需要同时配置 JXF_MAIL_HOST / JXF_MAIL_USER / "
            "JXF_MAIL_PASSWORD / JXF_MAIL_TO，或全部留空以禁用该通道"
        )
    cfg["mail_enabled"] = bool(present)

    # --- 通道 2：Portcloud Notify ---
    cfg.setdefault("pc_url", "https://notify.portcloud.online")
    cfg["pc_url"] = cfg["pc_url"].rstrip("/")
    # key 与 to 必须成对出现
    if bool(cfg.get("pc_key")) != bool(cfg.get("pc_to")):
        missing = "JXF_PC_TO" if cfg.get("pc_key") else "JXF_PC_KEY"
        raise SystemExit(
            f"Portcloud Notify 配置不完整，缺少：{missing}\n"
            "  需要同时配置 JXF_PC_KEY / JXF_PC_TO，或全部留空以禁用该通道"
        )
    cfg["pc_enabled"] = bool(cfg.get("pc_key") and cfg.get("pc_to"))

    if not (cfg["mail_enabled"] or cfg["pc_enabled"]):
        log("未配置任何通知通道，仅输出日志")

    if not cfg.get("username") or not cfg.get("password"):
        raise SystemExit(
            "缺少凭据：请复制 .env.example 为 .env 并填写，"
            "或设置环境变量 JXF_USERNAME / JXF_PASSWORD"
        )
    return cfg


# ---------------------------------------------------------------- 通知

def send_mail(cfg: dict, subject: str, body: str) -> bool:
    """通过 SMTP 发送通知。失败只记录，不影响签到结果。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["mail_user"]
    msg["To"] = cfg["mail_to"]
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(cfg["mail_host"], cfg["mail_port"],
                              timeout=cfg["request_timeout"]) as s:
            s.login(cfg["mail_user"], cfg["mail_password"])
            s.send_message(msg)
        log(f"  邮件已发送 → {cfg['mail_to']}")
        return True
    except Exception as e:
        # 通知失败不应让整个任务失败 —— 签到本身可能已经成功
        log(f"  邮件发送失败: {e}")
        return False


def send_portcloud(cfg: dict, subject: str, body: str) -> bool:
    """
    通过 Portcloud Notify 发送通知。

    该服务把业务失败表达为 HTTP 200 + success:false，因此不能只看状态码，
    必须解析响应体。429/503 是真正的传输层失败。
    """
    url = f"{cfg['pc_url']}/api/v1/send"
    payload = {"to": cfg["pc_to"], "subject": subject, "text": body}
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg['pc_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=cfg["request_timeout"],
        )
    except Exception as e:
        log(f"  Portcloud 请求异常: {e}")
        return False

    # 先尝试解析结构化错误
    try:
        d = r.json()
    except Exception:
        log(f"  Portcloud 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}")
        return False

    if d.get("success") is True:
        log(f"  Portcloud 已发送 → {cfg['pc_to']}"
            f"（message_id={d.get('message_id')}）")
        return True

    err = d.get("error") or {}
    code = err.get("code") or f"HTTP_{r.status_code}"
    msg = err.get("message") or r.text[:200]
    log(f"  Portcloud 发送失败: {code} — {msg}")
    return False


def notify(cfg: dict, ok: bool, summary: str) -> None:
    """按配置决定是否发送通知，并向所有已启用的通道投递。"""
    if ok and not cfg["notify_success"]:
        return
    if not ok and not cfg["notify_failure"]:
        return

    icon = "✓" if ok else "✗"
    subject = f"{icon} 签到{'成功' if ok else '失败'} — {cfg['username']}"
    body = (
        f"账号：{cfg['username']}\n"
        f"时间：{now():%Y-%m-%d %H:%M:%S}（北京时间）\n"
        f"结果：{summary}\n"
    )

    if cfg.get("mail_enabled"):
        send_mail(cfg, subject, body)
    if cfg.get("pc_enabled"):
        send_portcloud(cfg, subject, body)


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
    begin = (now().date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = today_str()
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
    ap.add_argument("--check", action="store_true", help="只显示状态，不签到")
    ap.add_argument("--force", action="store_true",
                    help="今天已有记录时仍强制提交一次")
    args = ap.parse_args()

    cfg = load_config()
    log(f"账号 {cfg['username']}")

    # 每次运行只通知一次。中途若已发送，结束时不再重复。
    sent = {"done": False}

    def finish(ok: bool, summary: str, fatal: bool = False) -> int:
        """
        ok    —— 业务结果，决定发成功通知还是失败通知
        fatal —— 脚本自身是否失败（登录不上、取不到坐标等），决定退出码

        退出码只反映脚本能否完成流程，不反映签到结果。业务结果通过
        邮件通知传达，因此「已签到」「被服务端拒绝」等都不算失败。
        """
        if not sent["done"]:
            sent["done"] = True
            notify(cfg, ok, summary)
        return 1 if fatal else 0

    sess = requests.Session()
    token = login(sess, cfg)
    if not token:
        return finish(False, "登录失败（验证码重试用尽或账号密码错误）",
                      fatal=True)

    today = today_str()
    recs = fetch_records(sess, cfg, token, today, today)

    if args.check:
        log(f"今天已有 {len(recs)} 条签到记录")
        for rec in recs:
            log(f"  id={rec.get('id')} 坐标=({rec.get('lng')}, {rec.get('lat')}) "
                f"地址={rec.get('address')} 时间={rec.get('createdAt')}")
        return finish(True, f"仅查询状态，今天已有 {len(recs)} 条签到记录")

    # dry-run 只观察不提交，不受预检查影响 —— 它本就不打算真的签到。
    if args.dry_run:
        try:
            lng, lat = resolve_coords(sess, cfg, token)
        except SystemExit as e:
            return finish(False, f"取坐标失败：{e}", fatal=True)
        log(f"[dry-run] 今天已有 {len(recs)} 条记录，"
            f"将提交坐标 ({lng}, {lat})，但不实际提交")
        return finish(True, f"[dry-run] 未实际提交（坐标 {lng}, {lat}）")

    # 预检查：今天已有记录时不提交。
    #
    # 业务上视为失败（发失败通知），因为脚本未执行签到动作，且已有记录
    # 是否被服务端认定无法由此接口判定，需人工核实。
    # 但退出码仍为 0 —— 脚本本身正常完成了流程，退出码不表达业务结果。
    # 要强制提交用 --force。
    if recs and not args.force:
        log(f"今天已有 {len(recs)} 条签到记录：")
        for rec in recs:
            log(f"  id={rec.get('id')} 坐标=({rec.get('lng')}, {rec.get('lat')}) "
                f"地址={rec.get('address')} 时间={rec.get('createdAt')} "
                f"isWarning={rec.get('isWarning')}")
        log("未提交签到（已有记录，无法确认其是否有效）")
        return finish(
            False,
            f"今天已有 {len(recs)} 条记录，未提交。"
            "请自行核实该记录是否有效，或加 --force 强制提交"
        )

    try:
        lng, lat = resolve_coords(sess, cfg, token)
    except SystemExit as e:
        return finish(False, f"取坐标失败：{e}", fatal=True)

    ok = do_signin(sess, cfg, token, lng, lat)
    summary = (f"签到成功（坐标 {lng}, {lat}）" if ok
               else "签到失败，请查看运行日志")
    return finish(ok, summary)


if __name__ == "__main__":
    sys.exit(main())
