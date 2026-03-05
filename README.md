# Domain Monitor v1.1 部署与启动说明

## 环境依赖
- Python 3.10+（推荐 3.12）
- PostgreSQL 12+（或兼容版本）
- Playwright（Chromium）与浏览器运行依赖
- 可选：Tesseract OCR（pytesseract 使用），用于截图中 IP 数量校验

## 安装
- 安装 Python 依赖

```bash
pip install -r requirements.txt
```

- 安装 Playwright 浏览器

```bash
python -m playwright install
```

## 配置
- 项目根目录创建 .env（已提供示例），关键字段：
  - DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
  - HEADLESS=true|false
  - DEFAULT_PROXY=scheme://host:port
  - USER_AGENT, REFERER, COOKIE
  - SCREENSHOT_DIR=./screenshots
- 代理格式示例：
  - http://127.0.0.1:7890
  - socks5://127.0.0.1:1080
  - 若需要认证，可扩展为 Playwright 代理参数携带 username/password（当前默认仅 server）

## 数据库
### monitor_targets
CREATE TABLE monitor_targets (
  id bigint NOT NULL DEFAULT nextval('monitor_targets_id_seq'::regclass),
  domain text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  priority smallint NOT NULL DEFAULT 0,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone,
  last_scheduled_at timestamp with time zone,
  schedule_interval_minutes integer NOT NULL DEFAULT 10
);
ALTER TABLE monitor_targets ADD CONSTRAINT chk_monitor_targets_domain_nonempty CHECK ((length(TRIM(BOTH FROM domain)) > 0));
ALTER TABLE monitor_targets ADD CONSTRAINT monitor_targets_pkey PRIMARY KEY (id);
ALTER TABLE monitor_targets ADD CONSTRAINT uq_monitor_targets_domain UNIQUE (domain);
CREATE INDEX idx_monitor_targets_enabled_last_scheduled ON public.monitor_targets USING btree (enabled, last_scheduled_at);
CREATE INDEX idx_monitor_targets_schedule_interval ON public.monitor_targets USING btree (schedule_interval_minutes);

### monitor_waiting_tasks
CREATE TABLE monitor_waiting_tasks (
  id bigint NOT NULL DEFAULT nextval('monitor_waiting_tasks_id_seq'::regclass),
  target_id bigint NOT NULL,
  domain text NOT NULL,
  status text NOT NULL DEFAULT 'waiting'::text,
  lease_until timestamp with time zone,
  worker_id text,
  attempts integer NOT NULL DEFAULT 0,
  error_message text,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now()
);
ALTER TABLE monitor_waiting_tasks ADD CONSTRAINT monitor_waiting_tasks_target_id_fkey FOREIGN KEY (target_id) REFERENCES monitor_targets(id) ON DELETE CASCADE;
ALTER TABLE monitor_waiting_tasks ADD CONSTRAINT monitor_waiting_tasks_pkey PRIMARY KEY (id);
CREATE INDEX idx_waiting_tasks_created ON public.monitor_waiting_tasks USING btree (created_at);
CREATE INDEX idx_waiting_tasks_status_lease ON public.monitor_waiting_tasks USING btree (status, lease_until);
CREATE INDEX idx_waiting_tasks_target ON public.monitor_waiting_tasks USING btree (target_id);
CREATE INDEX idx_waiting_tasks_updated ON public.monitor_waiting_tasks USING btree (updated_at);

### monitor_tasks
CREATE TABLE monitor_tasks (
  id bigint NOT NULL DEFAULT nextval('monitor_tasks_id_seq'::regclass),
  platform text NOT NULL,
  domain text NOT NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  status text NOT NULL,
  proxy_server text,
  headless boolean,
  count integer DEFAULT 0,
  browser_launch_ms double precision,
  collect_ms double precision,
  insert_ms double precision,
  total_ms double precision,
  error_type text,
  error_message text
);
ALTER TABLE monitor_tasks ADD CONSTRAINT monitor_tasks_pkey PRIMARY KEY (id);

### monitor_results
CREATE TABLE monitor_results (
  id bigint NOT NULL DEFAULT nextval('monitor_results_id_seq'::regclass),
  task_id bigint NOT NULL,
  operator text,
  region text,
  download_time double precision,
  connect_time double precision,
  dns_time double precision,
  total_time double precision,
  status_code text,
  ip_location text,
  response_ip text,
  raw jsonb,
  ip_country text,
  ip_province text,
  ip_city text,
  ip_isp text
);
ALTER TABLE monitor_results ADD CONSTRAINT monitor_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES monitor_tasks(id) ON DELETE CASCADE;
ALTER TABLE monitor_results ADD CONSTRAINT monitor_results_pkey PRIMARY KEY (id);

## 一次性检测（直接运行）
- 命令：

```bash
python main.py --domains google.com --platform itdog --headless
```

- 说明：
  - domains 支持多个（最多 5），可传入域名或完整 URL
  - --proxy 优先级高于 .env 的 DEFAULT_PROXY
  - 运行完成会在 monitor_tasks 与 monitor_results 中写入一条任务与对应结果
  - 截图会保存到 SCREENSHOT_DIR，控制台输出 JSON 统计摘要

## 简易调度（Producer）
- 从 monitor_targets 中挑选到期的目标，插入 monitor_waiting_tasks 为 waiting 状态
- 位置：task_producer/producer.py
- 示例：

```bash
python task_producer/producer.py --batch-size 100 --loop-seconds 120
```

- 说明：
  - 按 enabled=true 且 last_scheduled_at + schedule_interval_minutes 到期筛选
  - 已存在 waiting/leased 的同 target_id 不重复插入
  - 插入成功会将 monitor_targets.last_scheduled_at 更新为 NOW()

## Worker 执行者
- 从 monitor_waiting_tasks 抢占 waiting 任务并执行检测，完成后标记为 done/failed
- 位置：task_worker/worker.py
- 示例：

```bash
python task_worker/worker.py --batch-size 5 --lease-minutes 15 --poll-seconds 10 --headless
```

- 参数：
  - --batch-size 每轮抢占数量
  - --lease-minutes 租约时长（leased 期间其他实例不可抢占）
  - --poll-seconds 无任务时的轮询间隔

## 日志与截图
- 运行日志：根目录 run.log，同时打印到控制台
- 截图目录：SCREENSHOT_DIR（默认 ./screenshots），已在 .gitignore 中忽略
- 控制台摘要：每个域名打印 JSON，包括请求统计与耗时信息

## 代理说明
- 浏览器代理由 Playwright 在启动时设置；为保障审计，会尝试通过 httpx 走代理访问 https://httpbin.org/ip 获取出口 IP 并写入任务记录
- 代理不可用时会在日志中警告，但仍按配置尝试启用代理；若需认证代理，可扩展为传递 username/password

## 并发与扩展
- 简易调度架构基于数据库行级锁与租约，不引入队列中间件
- 多实例部署：直接运行多个 worker 进程或多台机器运行同脚本；FOR UPDATE SKIP LOCKED 保证不重复消费
- 建议在 monitor_targets 中设置合理的 schedule_interval_minutes，避免过度频繁调度

## 故障排查
- itdog 页面无法访问：日志会输出“请求 itdog 网站失败…请检查网络或者代理”，检查 DEFAULT_PROXY 或机器网络
- 没有新任务：eligible_targets=0，说明 last_scheduled_at+interval 尚未到期；可手动调整 last_scheduled_at 或调小间隔
- 代理与出口 IP：日志含 “detected real proxy ip”，若缺失或为 null，说明探测失败但不影响代理设置
