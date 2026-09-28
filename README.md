# fuck-jxf

> **⚠️ 强烈不建议使用。本项目仅供学习与技术研究，请勿用于真实考勤打卡。**
> 详见下方「免责声明」。

针对 `jlujxf.com` 小程序的一次技术研究记录：
如何分析其登录流程、识别算术验证码、并以 CI 定时任务的形式复现请求。

内容涵盖 HTTP 抓包分析、验证码 OCR、GitHub Actions 定时任务等知识点。

---

## ⚠️ 免责声明

### 强烈不建议使用本项目。仅供学习与技术研究。

本项目是一份**技术研究记录**，用于学习 HTTP 协议分析、验证码 OCR 识别、
CI/CD 定时任务等知识点。**它不应该被运行，尤其不应该被用来完成真实的考勤打卡。**

**请不要使用本项目。** 如果你正在考虑用它替代手动签到，请先读完下面这段：

- 自动打卡本质上是**规避考勤管理**。无论技术上看是否可行，它都可能违反你所在
  学校或单位的规章制度，并可能导致**纪律处分、成绩认定问题或其他你并不想承担的后果**。
- 考勤系统的存在有其管理目的。用脚本绕过它，风险由**你本人**承担，而不是由本项目承担。
- 即使只用于你自己的账号，上述风险依然存在。「没影响别人」不等于「没有后果」。

其他限制：

- 本项目**仅限用于你本人拥有且有权访问的账号**，严禁用于任何他人账号或系统。
- 严禁用于商业用途、批量刷签、代他人签到或任何绕过考勤管理的行为。
- 严禁将本项目用于任何未授权的安全测试或渗透行为。
- 使用者应自行承担使用本项目所产生的一切后果，包括但不限于因违反所在学校/单位
  的规章制度、服务条款或相关法律法规而产生的责任。
- 本项目作者**不对任何滥用行为负责**，也不对使用本项目造成的任何直接或间接损失负责。
- 如果你不是相关系统的授权使用者，请立即停止使用并删除本项目。

**使用本项目即表示你已阅读、理解并自愿承担上述全部风险与责任。**

> **关于技术发现的说明**：本项目的技术实现完全基于对公开可访问接口的实测行为。
> 在开发过程中发现该后端存在若干安全缺陷（如 `/getInfo` 接口回传密码哈希、
> 接口无签名、验证码为简单算术题等）。这些发现**不构成对目标系统进行未授权测试的理由**，
> 也不代表本项目鼓励任何人去利用它们。如需报告安全问题，请通过正规渠道联系系统所有者。
>
> 本项目公开的目的，是作为「一个缺乏防自动化设计的后端能被怎样分析」的**反面教材**，
> 而不是提供一件可用的工具。

---

## 一、GitHub Actions 定时执行

### 1. Fork 本仓库

点击本仓库右上角的 **Fork**，把它复制到你自己的账号下。

> **为什么必须 Fork，而不是直接用本仓库？**
> 定时任务需要读取 `JXF_USERNAME` / `JXF_PASSWORD` 两个 secrets，
> 而 secrets 只能配置在**你自己拥有写权限的仓库**里。
> 你没有本仓库的写权限，因此无法在本仓库配置 secrets。

### 2. 启用 Actions（关键，容易漏）

Fork 之后，GitHub **默认不运行 fork 仓库里的 workflow**，定时任务不会执行。
进入你 fork 出来的仓库，点 **Actions** 标签页，若看到提示则点击
**I understand my workflows, go ahead and enable them**。

> 这是 GitHub 的官方行为：*"Workflows don't run in forked repositories by default."*
> 参考 [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。

启用后建议先手动跑一次确认能跑通（见下文「手动触发」）。

### 3. 配置 Secrets

进入**你 fork 的仓库**，点
**Settings → Secrets and variables → Actions → New repository secret**，
添加以下 secrets：

**必填**

| Secret 名称 | 说明 |
|---|---|
| `JXF_USERNAME` | 登录账号（学号） |
| `JXF_PASSWORD` | 登录密码 |

**可选：邮件通知**

不配置则不发邮件。要启用需**同时配置**下面四项，缺一不可（配置不完整会直接报错退出）：

| Secret 名称 | 说明 |
|---|---|
| `JXF_MAIL_HOST` | SMTP 服务器，如 `smtp.qq.com`、`smtp.163.com` |
| `JXF_MAIL_USER` | 发件邮箱 |
| `JXF_MAIL_PASSWORD` | 邮箱**授权码**（不是登录密码） |
| `JXF_MAIL_TO` | 收件邮箱 |

**可选：通知开关**

| Secret 名称 | 默认 | 说明 |
|---|---|---|
| `JXF_MAIL_NOTIFY_SUCCESS` | `false` | 签到成功时是否发邮件 |
| `JXF_MAIL_NOTIFY_FAILURE` | `true` | 签到失败时是否发邮件 |
| `JXF_MAIL_PORT` | `465` | SMTP 端口，使用 SSL |

布尔值接受 `true/false`、`yes/no`、`1/0`、`on/off`（不区分大小写）。
**写错会直接报错退出**，不会静默取默认值。

> 默认只在**失败时**发邮件 —— 签到成功是常态，每周收一封成功邮件意义不大。
> 如果你希望每次都有回执，把 `JXF_MAIL_NOTIFY_SUCCESS` 设为 `true`。

**授权码怎么拿**：以 QQ 邮箱为例，设置 → 账户 → 开启 SMTP 服务 →
生成授权码。163 邮箱类似。**注意授权码不是邮箱登录密码。**

> **坐标无需配置。** 脚本会自动复用最近一次成功签到的坐标，
> 以保证每次提交的位置完全一致。
> **若该账号从未签到过，脚本会报错退出** —— 请先在小程序里手动签到一次。

> 再次提醒：以下步骤仅作为技术流程说明记录，**不代表建议你去执行**。

### 4. 触发时间

已配置为 **每周一北京时间 09:00**（UTC 周一 01:00）自动执行：

```yaml
on:
  schedule:
    - cron: '0 1 * * 1'   # UTC 周一 01:00 = 北京周一 09:00
```

> **GitHub Actions 的 cron 使用 UTC 时区。**
> 另外，定时任务在平台高负载时可能延迟数分钟至一小时，属正常现象。

#### 想改打卡时间？

打卡时间**不支持用 Secrets 或环境变量配置，需要自己改代码后提交**。

1. 把仓库 clone 到本地（或直接在 GitHub 网页上编辑）
2. 修改 `.github/workflows/signin.yml` 里的 cron 表达式
3. 提交并推送到默认分支

**北京时间 = UTC + 8**，所以 `北京 09:00` 要写成 `UTC 01:00`：

```yaml
- cron: '0 1 * * 1'      # 周一 09:00 北京（默认）
- cron: '0 10 * * 1'     # 周一 18:00 北京
- cron: '0 1 * * *'      # 每天 09:00 北京
- cron: '0 1 * * 1,3,5'  # 周一/三/五 09:00 北京
```

### 5. 手动触发

**Actions → 自动签到 → Run workflow**，可勾选 `dry_run` 只走流程不实际提交。

### 6. 查看结果

每次运行的结果会写入 **Actions 运行详情页的 Summary**（签到日志最后 20 行）。

### 7. 保活（keepalive）

GitHub 会在仓库 60 天没有活动时自动停用定时任务。签到仓库平时不会有
commit，所以会触发这条规则。

仓库内的 `.github/workflows/keepalive.yml` 会**每 30 天自动提交一次
commit**，让仓库保持活动状态，避免定时任务被停用。

> Fork 后请确认 `keepalive.yml` 也处于启用状态。

---

## 二、本地运行

```bash
cd ~/dev/fuck-jxf

# 1. 配置凭据
cp .env.example .env
vim .env                       # 填入学号、密码

# 2. 运行
.venv/bin/python signin.py
```

`.env` 已被 gitignore，不会提交。首次使用需先建虚拟环境：

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
```

### 命令行参数

```bash
python signin.py            # 签到（今天已签则跳过）
python signin.py --check    # 只查状态，不签到
python signin.py --dry-run  # 走完整流程但不提交
python signin.py --force    # 今天已签也再提交一次
```

> 需要定时执行请使用 GitHub Actions（见上文），无需本地常驻进程。

---

## 配置项

本地写在 `.env`，CI 里配在 GitHub Secrets —— **变量名完全一致**，
所以本地跑通即代表 CI 能跑通。

**必填**

| 变量 | 说明 |
|---|---|
| `JXF_USERNAME` | 登录账号（学号） |
| `JXF_PASSWORD` | 登录密码 |

**可选：邮件通知**（不配置则不发邮件）

| 变量 | 默认 | 说明 |
|---|---|---|
| `JXF_MAIL_HOST` | — | SMTP 服务器 |
| `JXF_MAIL_USER` | — | 发件邮箱 |
| `JXF_MAIL_PASSWORD` | — | 邮箱授权码 |
| `JXF_MAIL_TO` | — | 收件邮箱 |
| `JXF_MAIL_PORT` | `465` | SMTP 端口（SSL） |
| `JXF_MAIL_NOTIFY_SUCCESS` | `false` | 成功时是否发邮件 |
| `JXF_MAIL_NOTIFY_FAILURE` | `true` | 失败时是否发邮件 |

坐标**不需要配置**，脚本自动复用最近一次成功签到的坐标。

环境变量的优先级高于 `.env` 文件。

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `signin.py` | 主程序 |
| `.env.example` | 凭据模板（`cp .env.example .env` 后填写） |
| `.env` | 你的凭据（已被 gitignore） |
| `requirements.txt` | Python 依赖 |
| `.github/workflows/signin.yml` | 定时签到（每周一北京时间 09:00） |
| `.github/workflows/keepalive.yml` | 保活，防定时任务被自动停用（每月 1 日） |
| `signin.log` | 运行日志（已被 gitignore） |
| `.keepalive` | 保活时间戳，由 keepalive.yml 自动更新 |

---

## 常见问题

**Q: Actions 里报 `缺少凭据`？**
A: 检查 secrets 名称是否为 `JXF_USERNAME` / `JXF_PASSWORD`，注意大小写。

**Q: 报 `libGL.so.1: cannot open shared object file`？**
A: workflow 里已包含 `apt-get install libgl1 libglib2.0-0`，若仍报错请检查该步骤是否执行成功。

**Q: 定时任务没按时执行？**
A: 按可能性排查：
1. **workflow 被自动停用**（最常见）—— 去 Actions 页面看是否有 disabled 横幅，点 Enable 恢复。详见上文「7. 保活」。
2. **Fork 仓库的 Actions 未启用** —— 见上文「2. 启用 Actions」。
3. **cron 延迟** —— GitHub Actions 在平台高负载时会延迟，通常数分钟内，偶尔更久。这是平台特性，无法从代码层面解决。

**Q: 没收到通知邮件？**
A: 依次检查：
1. `JXF_MAIL_NOTIFY_SUCCESS` 默认为 `false`，成功时本来就不发信
2. 四个必填项（HOST/USER/PASSWORD/TO）是否都配了，缺一个会直接报错
3. `JXF_MAIL_PASSWORD` 填的是**授权码**还是登录密码 —— 必须是授权码
4. 收件箱的垃圾邮件目录
5. Actions 日志里搜「邮件」看是否有发送失败的提示

**Q: 验证码识别失败？**
A: 脚本默认重试 6 次（每次重新获取验证码）。若持续失败，可能是验证码机制变更，需要调整 OCR 逻辑。
