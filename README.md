# 🛍️ iOS 风格积分商城（公网 + 云端计时）

一个纯标准库实现的积分商城 Web 应用，UI 走干净精致的 **iOS 风格（无丑陋毛玻璃）**，通过 **Cloudflare 免登录临时隧道**暴露到公网，并用 **GitHub Actions 定时任务（云端计时）** 让临时隧道长期在线。

## ✨ 功能

- **注册 / 登录**：邮箱 + 密码注册；登录支持「密码登录」与「邮箱验证码登录」两种方式（无密码也可）
- **自制人机验证**：点选顺序验证（非 Cloudflare Turnstile），挡在发码前面
- **发码 60 秒限频**：防止邮箱被风控
- **积分体系**：每日签到（连续签到加成）、每日任务赚积分
- **积分兑换**：商品兑换、兑换记录
- **管理员后台**：`Admin` 可自定义上架商品；**卡密类商品**可批量上传卡密，用户兑换时自动下发一条
- **公网访问**：Cloudflare 免登录临时隧道，零账号
- **云端计时**：GitHub Actions 定时任务反复拉起隧道，保持在线

## 🚀 本地运行

```bash
pip 不需要（零依赖，仅用 Python 标准库）
cp .env.example .env        # 填入邮箱 SMTP 与管理员种子
python3 server.py           # 默认 http://localhost:8000
```

> 未配置 `EMAIL_PASS` 时进入**开发模式**：发验证码会直接在接口回显（`dev_code`），方便本地调试；配置后才会真发邮件。

## 🔐 环境变量（或 GitHub Secrets）

| 变量 | 说明 |
| --- | --- |
| `SMTP_HOST` / `SMTP_PORT` | 邮箱 SMTP 地址（默认 `smtp.yeah.net:465`） |
| `EMAIL_USER` / `EMAIL_PASS` | 发件邮箱与**授权码**（不是邮箱登录密码） |
| `SMTP_FROM` | 发件人（默认同 `EMAIL_USER`） |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | 管理员种子账号，每次启动自动确保为 Admin |

> `.env` 已写入 `.gitignore`，**绝不进入公开仓库**。部署到 GitHub 时请配置为仓库 **Secrets**。

## ☁️ 公开仓库 + 云端计时（永久隧道）

1. 把本仓库推到 GitHub 并设为 **Public**
2. 在仓库 `Settings → Secrets and variables → Actions` 添加上面的 Secrets
3. 在 `Actions` 页手动 `Run workflow`，或推送 `main` 分支即可触发
4. 工作流会：启动商城 → 下载 cloudflared → 拉起临时隧道 → 把最新公网地址写到 **Issue** 与运行摘要

> 临时隧道地址每次重建都会变化；GitHub Actions 每 15 分钟兜底 + 任务接力，使商城长期保持在线（公开仓库 Linux 分钟数免费无限）。

## 📁 结构

```
server.py                  纯标准库后端（接口 + 邮件 + 人机验证 + 管理员）
public/index.html          iOS 风格前端（登录/注册/商城/任务/我的/管理）
.env.example               环境变量模板
.github/workflows/tunnel.yml  云端计时工作流
```

## ⚠️ 说明

- 临时隧道地址会变；如需固定域名需 Cloudflare 账号（命名隧道），本方案刻意使用**免登录临时隧道**。
- 运行数据（`data.json`，含积分/兑换/密码哈希）存于运行实例本地；GitHub Actions 运行器为临时环境，重启会重置——如需持久化可把 `data.json` 提交回仓库（注意隐私）或换用常驻主机。
