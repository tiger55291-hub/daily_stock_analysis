begin;

create table if not exists public.lens_gemini_candles_1m (
    closed_at timestamptz primary key,
    open numeric not null,
    high numeric not null,
    low numeric not null,
    close numeric not null,
    check (low > 0 and low <= least(open, close) and high >= greatest(open, close))
);

create table if not exists public.lens_gemini_signals (
    signal_id text primary key,
    candle_time timestamptz not null,
    symbol text not null check (symbol = 'CAPITALCOM:SPX500'),
    pattern text not null check (pattern in ('GAMMA_BOUNCE', 'FAKEOUT')),
    side text not null check (side in ('CALL', 'PUT')),
    entry numeric not null,
    stop numeric not null,
    target numeric not null,
    status text not null default 'received' check (status in (
        'received', 'rejected', 'failed', 'expired', 'ambiguous', 'sent'
    )),
    sent_at timestamptz,
    telegram_message_id bigint,
    outcome_status text check (outcome_status in ('win', 'loss', 'timeout', 'incomplete')),
    outcome_at timestamptz,
    check ((side = 'CALL' and stop < entry and entry < target)
        or (side = 'PUT' and target < entry and entry < stop))
);

create index if not exists lens_gemini_open_signals
    on public.lens_gemini_signals (candle_time)
    where status = 'sent' and outcome_status is null;

alter table public.lens_gemini_signals enable row level security;
alter table public.lens_gemini_candles_1m enable row level security;
revoke all on public.lens_gemini_signals, public.lens_gemini_candles_1m from public, anon, authenticated;
grant select, insert, update on public.lens_gemini_signals to service_role;
grant select, insert, delete on public.lens_gemini_candles_1m to service_role;

create or replace function public.reconcile_lens_gemini()
returns void
language sql
set search_path = ''
as $$
    with open_signals as (
        select s.*,
           -- Use only full minute bars whose start is at or after Telegram delivery.
           date_trunc('minute', s.sent_at) +
              case when s.sent_at = date_trunc('minute', s.sent_at)
                   then interval '0' else interval '1 minute' end as observation_start
        from public.lens_gemini_signals s
        where s.status = 'sent' and s.outcome_status is null and s.sent_at is not null
    ), evaluated as (
        select s.signal_id, hit.outcome, hit.closed_at, hit.complete_count,
               s.observation_start + interval '60 minutes' as deadline
        from open_signals s
        cross join lateral (
            select (array_agg(binned.outcome order by binned.closed_at)
                        filter (where binned.outcome is not null))[1] as outcome,
                   min(binned.closed_at) filter (where binned.outcome is not null) as closed_at,
                   count(*) as complete_count
            from (
                select b.closed_at, row_number() over (order by b.closed_at) as bar_number,
                    case
                        when (s.side = 'CALL' and b.low <= s.stop)
                          or (s.side = 'PUT' and b.high >= s.stop) then 'loss'
                        when (s.side = 'CALL' and b.high >= s.target)
                          or (s.side = 'PUT' and b.low <= s.target) then 'win'
                    end as outcome
                from public.lens_gemini_candles_1m b
                where b.closed_at > s.observation_start
                  and b.closed_at <= s.observation_start + interval '60 minutes'
            ) binned
            where binned.closed_at = s.observation_start + binned.bar_number * interval '1 minute'
        ) hit
    )
    update public.lens_gemini_signals s
       set outcome_status = coalesce(e.outcome, case when e.complete_count = 60 then 'timeout' else 'incomplete' end),
           outcome_at = coalesce(e.closed_at, e.deadline)
      from evaluated e
     where s.signal_id = e.signal_id and s.outcome_status is null
       and (e.outcome is not null or e.complete_count = 60
            or now() >= e.deadline + interval '10 minutes');

    update public.lens_gemini_signals
       set status = 'expired'
     where status = 'received' and candle_time < now() - interval '10 minutes';

    delete from public.lens_gemini_candles_1m where closed_at < now() - interval '14 days';
$$;

revoke all on function public.reconcile_lens_gemini() from public, anon, authenticated;
grant execute on function public.reconcile_lens_gemini() to service_role;

create or replace view public.lens_gemini_stats with (security_invoker = true) as
select periods.period, date_trunc(periods.unit, s.candle_time at time zone 'America/New_York')::date as period_start,
       s.symbol, s.pattern, s.side,
       count(*) filter (where s.status = 'sent') as sent,
       count(*) filter (where s.status = 'rejected') as rejected,
       count(*) filter (where s.status in ('failed', 'expired', 'ambiguous')) as delivery_errors,
       count(*) filter (where s.status = 'sent' and s.outcome_status is null) as open,
       count(*) filter (where s.outcome_status = 'win') as wins,
       count(*) filter (where s.outcome_status = 'loss') as losses,
       count(*) filter (where s.outcome_status = 'timeout') as timeouts,
       count(*) filter (where s.outcome_status = 'incomplete') as incomplete,
       round(100.0 * count(*) filter (where s.outcome_status = 'win')
                  / nullif(count(*) filter (where s.outcome_status in ('win', 'loss')), 0), 2) as win_rate_pct
  from public.lens_gemini_signals s
  cross join (values ('daily', 'day'), ('weekly', 'week')) as periods(period, unit)
 group by 1, 2, 3, 4, 5;

revoke all on public.lens_gemini_stats from public, anon, authenticated;
grant select on public.lens_gemini_stats to service_role;

commit;
