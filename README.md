# UNGM 每日投标机会监控

这个项目每天监控 UNGM 公开采购机会，并通过邮件发送符合条件的货物采购项目。

筛选条件：

- 发布时间在最近 3 天内
- 截止日期距离当天至少还有 10 天
- 符合轻工业产品制造/供货范围
- 排除咨询、培训、维护、建筑服务、研究、审计、调研、评估、招聘、IT 服务、物流运输、活动执行等服务类项目
- 已发送过的 notice id 不会重复发送

## 环境配置

需要 Python 3.11。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## SMTP 配置

程序通过环境变量读取 SMTP 配置。当前 Gmail 插件连接的账号是 `ld1536487199@gmail.com`，使用 Gmail SMTP 时配置如下：

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="ld1536487199@gmail.com"
export SMTP_PASSWORD="你的 Google App Password"
export MAIL_FROM="ld1536487199@gmail.com"
export MAIL_TO="ld1536487199@gmail.com"
```

`MAIL_TO` 支持多个收件人，用英文逗号分隔：

```bash
export MAIL_TO="one@example.com,two@example.com"
```

端口 `465` 会使用 SMTP SSL；其他端口默认使用 STARTTLS。

Gmail 账号通常不能直接使用网页登录密码作为 SMTP 密码。请在 Google 账号中开启两步验证，然后进入 `Security` -> `App passwords` 生成应用专用密码，把生成的 16 位密码填入 `SMTP_PASSWORD`。GitHub Actions 部署时请把该值保存为仓库 Secret，不要提交到代码仓库。

## 本地运行

正常运行：

```bash
python ungm_watch.py
```

只抓取和筛选，不发送邮件、不写入已发送状态：

```bash
python ungm_watch.py --dry-run
```

调整分页数量：

```bash
python ungm_watch.py --max-pages 30
```

使用可见浏览器窗口调试：

```bash
python ungm_watch.py --headful --dry-run
```

`sent_ids.json` 用于保存已发送过的 notice id。文件不存在时程序会自动创建。

## 部署到 GitHub Actions

工作流文件位于 `.github/workflows/ungm-watch.yml`。

部署步骤：

1. 将项目推送到 GitHub 仓库。
2. 在仓库设置里进入 `Settings` -> `Secrets and variables` -> `Actions`。
3. 添加以下 Repository secrets：
   - `SMTP_HOST`
   - `SMTP_PORT`
   - `SMTP_USER`
   - `SMTP_PASSWORD`
   - `MAIL_FROM`
   - `MAIL_TO`
4. 工作流会每天 UTC 01:00 运行，即北京时间 09:00。
5. 也可以在 GitHub Actions 页面手动点击 `Run workflow` 立即运行。

工作流会：

- 安装 Python 3.11
- 安装依赖
- 安装 Playwright Chromium
- 执行 `python ungm_watch.py`
- 使用 GitHub Actions cache 保存 `sent_ids.json`，避免跨天重复发送

## 日志与稳定性

程序会输出关键步骤日志，包括：

- 打开 UNGM 页面
- 每页抓取到的项目数量
- 详情页解析状态
- 每条项目的过滤原因或匹配结果
- 邮件发送结果
- `sent_ids.json` 保存结果

如果某条详情页解析失败，程序会记录警告并继续处理其他项目。如果 SMTP 配置缺失或邮件发送失败，程序不会更新 `sent_ids.json`，避免把未发送项目误标记为已发送。
