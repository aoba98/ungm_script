# UNGM 每日投标机会监控

这个项目每天监控 UNGM 公开采购机会，并通过邮件发送符合条件的轻工业产品制造/供货类采购项目。

筛选条件：

- 截止日期距离当天至少还有 10 天
- 发布时间在最近 3 天内
- 符合轻工业产品制造/供货范围，包括文具、办公用品、学校用品、玩具、体育用品、箱包、塑料制品、纺织品、帐篷、家庭用品、服装、礼品、儿童用品、教育用品等
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

## 动态页面加载策略

UNGM 列表页是动态加载页面，程序使用 Playwright Chromium 渲染页面后再提取数据。为了降低漏抓概率，脚本会：

- 等待网络空闲和采购机会列表出现
- 在页面搜索表单中设置 `Published between` 为最近 3 天、`Deadline between` 为当天 + 10 天到两年后，先让 UNGM 返回更接近目标的结果；如果 UNGM 前端筛选返回空结果，会自动回退到未筛选列表，再用脚本本地过滤发布时间和截止日期
- 自动滚动列表页，读取 `Displaying results ... of ...` 的总数并尽量加载完整结果集
- 多次检查 notice 数量、首尾 notice id 和结果总数，等待列表稳定后再提取
- 点击下一页后等待 notice 列表签名变化，避免读取上一页旧内容
- 输出分页诊断日志，包括当前页行数、notice 数量、首尾 notice id、下一页按钮候选信息
- 先按发布时间、截止日期、已发送状态和明显服务类采购类型做初筛，再小并发打开候选项目详情页并自动滚动后解析描述、日期、机构、国家和采购类型

如果找不到可用的下一页按钮、浏览器端筛选返回空结果，或当前页没有提取到 notice，程序会在 `debug/` 目录保存当前页面 HTML 和截图，方便排查 UNGM 页面结构变化。`debug/` 已加入 `.gitignore`，不会被提交；GitHub Actions 会把该目录作为短期 artifact 上传，保留 7 天。

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
