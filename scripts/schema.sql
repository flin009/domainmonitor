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
