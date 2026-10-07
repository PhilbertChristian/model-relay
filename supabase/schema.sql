-- Relay telemetry: every model call, switch, blocker and session summary.
-- Run in the Supabase SQL editor, then set SUPABASE_URL + SUPABASE_KEY (service role or an insert-only key).
create table if not exists relay_events (
  id         bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  session    text not null,
  host       text not null,          -- Agent37 instance id, or "local"
  event      text not null,          -- session_start | call | switch | blocker | provider_error | tool | session_end
  model      text,
  data       jsonb not null
);
create index if not exists relay_events_session on relay_events (session, created_at);

alter table relay_events enable row level security;
-- Allow anon inserts so the harness can log with the anon key; reads stay private.
create policy "relay insert" on relay_events for insert to anon with check (true);

-- Handy views for a dashboard
create or replace view relay_model_usage as
select model,
       count(*)                                   as calls,
       sum((data->>'input_tokens')::int)          as input_tokens,
       sum((data->>'output_tokens')::int)         as output_tokens,
       round(sum((data->>'usd')::numeric), 6)     as usd
from relay_events where event = 'call' group by model order by usd desc;

create or replace view relay_switches as
select created_at, session, host, data->>'old' as from_model, model as to_model, data->>'reason' as reason
from relay_events where event = 'switch' order by created_at desc;
