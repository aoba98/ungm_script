# UNGM 每日投标机会监控

这个项目每天监控 UNGM 公开采购机会，并通过邮件发送符合条件的轻工业产品制造/供货类采购项目。

筛选条件：

- 截止日期距离当天至少还有 10 天
- 发布时间在最近 3 天内
- 符合轻工业产品制造/供货范围，包括文具、办公用品、学校用品、玩具、体育用品、箱包、塑料制品、纺织品、帐篷、家庭用品、服装、礼品、儿童用品、教育用品等
- 本公司设立在 China；若投标机会强制要求本地供应商、本地公司或本地供货商，则过滤
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

## DeepSeek 配置

程序支持使用 DeepSeek API 判断 notice 是否符合公司业务范围。未配置 `DEEPSEEK_API_KEY` 时，会自动回退到原有关键词规则，不会影响每日运行。

```bash
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
export DEEPSEEK_MODEL="deepseek-v4-flash"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_TIMEOUT_SECONDS="20"
export DEEPSEEK_CONCURRENCY="4"
```

DeepSeek 判断只接收低噪声字段，包括标题、参考号、采购类型、机构、国家和详情描述，不使用整页详情文本，避免页面通用文案影响判断。API 调用超时、限流、返回空内容或 JSON 解析失败时，程序会对该 notice 回退到原有关键词规则，并在日志和匹配理由中标注 `DeepSeek fallback`。

## 本地运行

正常运行（UNGM 当前会对无界面 Chromium 返回 HTTP 403，因此使用普通浏览器模式）：

```bash
python ungm_watch.py --headful
```

只抓取和筛选，不发送邮件、不写入已发送状态：

```bash
python ungm_watch.py --headful --dry-run
```

调整分页数量：

```bash
python ungm_watch.py --headful --max-pages 30
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
- 先按发布时间、截止日期、已发送状态和明显服务类采购类型做初筛，再复用同一浏览器会话顺序读取详情页 HTML；详情已经由服务器渲染，不再为每条项目重复加载脚本、图片和字体
- 详情请求之间至少间隔 2 秒；收到 HTTP 429 时暂停整个详情队列，遵循 `Retry-After`（缺失或无效时等待 60 秒），最多重试两次；持续限流或服务器要求等待超过 5 分钟时终止本次运行，保留未发送状态
- 列表完整性检查区分网页重复行和漏抓；详情描述优先按页面结构解析，站方明确发布的空描述与页面无法解析分别记录

如果找不到可用的下一页按钮、浏览器端筛选返回空结果，或当前页没有提取到 notice，程序会在 `debug/` 目录保存当前页面 HTML 和截图，方便排查 UNGM 页面结构变化。`debug/` 已加入 `.gitignore`，不会被提交。为避免公开工作流泄露原始页面信息，GitHub Actions 只上传允许列表处理后的 `debug/ungm-access.json`，保留 7 天，不上传原始 HTML、截图、cookies 或 HAR。

### 入口访问诊断（不发送邮件）

```bash
python ungm_access.py
```

该独立命令只尝试一次 UNGM 列表入口请求，禁止后续跳转、子资源和脚本，不加载 `.env`，不调用 AI/SMTP，不读取或更新 `sent_ids.json`。报告仅包含 HTTP 状态、安全 URL、允许列表响应头值和固定正文分类标签，不包含正文原文、页面标题、标识符或认证数据。HTTP 非 2xx 或无法获得响应仍返回非零退出码；2xx 只表示入口可访问，不代表完整抓取成功。普通监控遇到入口 HTTP 错误时也会先记录同样的安全摘要，再失败告警。

为严格禁止自动重定向，独立诊断使用 Playwright 的 `route.fetch(max_redirects=0, max_retries=0)` 获取一次响应，然后中止原浏览器导航，不渲染响应。报告注明 `playwright-route-fetch`，其 HTTP/TLS 实现不等同于正常 Chromium 导航；正常监控报告注明 `chromium-navigation`。两者结果不同不能直接归因于某种封禁或恢复。

一次性诊断发布可在提交信息加入 `[ungm-diagnostic]`：该 push 只运行无 SMTP/AI secrets 的诊断 job，跳过生产监控，重跑同一 run 也不会再次探测。后续定时及手动运行仍执行原监控，不受提交标记影响。诊断不使用代理、伪装、验证码绕过或重试；若站方明确拒绝访问，应通过官方支持确认允许的访问方式。

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
   - `DEEPSEEK_API_KEY`（可选；缺失时使用关键词规则 fallback）
4. 工作流会每天 UTC 00:55 运行，即北京时间 08:55（GitHub 调度可能延迟）。
5. 也可以在 GitHub Actions 页面手动点击 `Run workflow` 立即运行。默认勾选 `dry_run`，只验证抓取和关键词筛选，不调用 AI、不发送邮件、不更新已发送状态或缓存；抓取不完整时返回失败。取消勾选后执行正常监控。定时运行和普通 push 仍执行正常监控。

工作流会：

- 安装 Python 3.11
- 安装依赖
- 安装 Playwright Chromium
- 通过 `xvfb-run --auto-servernum python ungm_watch.py --headful` 使用普通 Chromium，虚拟显示器由 Playwright 的 Linux 依赖提供；列表浏览器和详情请求会话都使用该模式，避免无界面模式的入口 HTTP 403
- 正常运行时，如配置 DeepSeek key，会使用 DeepSeek 判断业务匹配范围
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

### 异常提醒

异常提醒沿用现有 `MAIL_TO` 收件人及 SMTP 配置，无需新增凭据：

- AI 部分或全部失败（包括 HTTP 402、网络、超时和返回格式异常）时，仍使用原关键词规则，但邮件主题带 `[异常提醒]`，列出失败数 / 尝试数、错误分类及回退命中 / 未命中数。API key 缺失也会明确提醒仅使用关键词规则。
- 列表等待超时、未完整加载、翻页未更新、抓取数少于页面总数、无法确认空结果或达到分页上限，以及详情抓取失败，都会进入同一封异常摘要。没有匹配项目时不再发送正常的“今天没有发现”结论。
- 抓取、筛选或状态保存中断会尝试发送 `[运行失败]` 邮件，并保留失败退出状态。工作流另有使用 Python 标准库的兜底邮件步骤，可以覆盖依赖安装、浏览器安装和脚本异常退出；已成功发送的失败提醒不会重复发送。
- 邮件投递失败或有收件人被拒收，不会把本次项目标记为已发送。若 SMTP 本身不可用，异常邮件也可能无法送达，请查看 GitHub Actions 的失败状态；执行器不可用或整个任务超时/取消时，任务内的邮件步骤同样无法保证运行。
- 仍然只处理最近 3 天发布、符合现有筛选规则且未发送的项目，不扩大时间范围、不补查历史、不重置已发送 ID。
- 这些提醒运行在同一个 GitHub Actions 工作流内；如果排程再次因仓库长期无活动而被停用，或执行器没有启动，工作流内无法自行发出停跑提醒，需要在 Actions 页面确认排程状态。

运行不连接网络、不发送真实邮件的回归测试：

```bash
python -m unittest discover -s tests -v
```


