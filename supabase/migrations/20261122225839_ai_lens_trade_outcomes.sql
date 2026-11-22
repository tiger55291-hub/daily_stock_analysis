alter table public.signals_log
  add column outcome_status text check (outcome_status in ('open', 'win', 'loss')),
  add column outcome_last_candle_at timestamptz,
  add column outcome_closed_at timestamptz,
  add column outcome_both_touched boolean;

create index lens_open_signals on public.signals_log (symbol, candle_time)
where outcome_status = 'open' and status = 'sent';

create table public.lens_candles_5m (
  symbol text not null,
  closed_at timestamptz not null,
  previous_at timestamptz not null check (previous_at < closed_at),
  previous_verified boolean not null,
  high numeric not null check (high > 0),
  low numeric not null check (low > 0 and low <= high),
  primary key (symbol, closed_at)
);

create index lens_candles_predecessor on public.lens_candles_5m (symbol, previous_at, closed_at);
alter table public.lens_candles_5m enable row level security;
revoke all on public.lens_candles_5m from public, anon, authenticated;
grant select, insert, update, delete on public.lens_candles_5m to service_role;

create function public.reconcile_lens_trades(p_symbol text)
returns void language plpgsql security invoker set search_path = '' as $$
declare
  trade record;
  bar record;
  cursor_at timestamptz;
  result text;
  hit_stop boolean;
  hit_target boolean;
begin
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('ai-lens-5m'), pg_catalog.hashtext(p_symbol));
  for trade in
    select s.signal_id, s.candle_time, s.outcome_last_candle_at, s.event_type, s.stop_price, s.target_1
    from public.signals_log s
    where s.symbol = p_symbol and s.timeframe = '5' and s.status = 'sent'
      and s.outcome_status = 'open' and s.event_type in ('LONG_SETUP', 'SHORT_SETUP')
    for update
  loop
    cursor_at := coalesce(trade.outcome_last_candle_at, trade.candle_time);
    result := 'open';
    hit_stop := false;
    hit_target := false;
    loop
      select b.closed_at, b.high, b.low into bar
      from public.lens_candles_5m b
      where b.symbol = p_symbol and b.previous_at = cursor_at
      order by b.closed_at limit 1;
      exit when not found;
      cursor_at := bar.closed_at;
      if trade.event_type = 'LONG_SETUP' then
        hit_stop := bar.low <= trade.stop_price;
        hit_target := bar.high >= trade.target_1;
      else
        hit_stop := bar.high >= trade.stop_price;
        hit_target := bar.low <= trade.target_1;
      end if;
      if hit_stop or hit_target then
        result := case when hit_stop then 'loss' else 'win' end;
        exit;
      end if;
    end loop;
    if cursor_at > coalesce(trade.outcome_last_candle_at, trade.candle_time) then
      update public.signals_log
      set outcome_status = result, outcome_last_candle_at = cursor_at,
          outcome_closed_at = case when result <> 'open' then cursor_at end,
          outcome_both_touched = case when result <> 'open' then hit_stop and hit_target end
      where signal_id = trade.signal_id;
    end if;
  end loop;
end;
$$;

create function public.record_lens_candles(p_symbol text, p_bars jsonb)
returns void language plpgsql security invoker set search_path = '' as $$
declare
  last_at timestamptz;
  latest_at timestamptz;
begin
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('ai-lens-5m'), pg_catalog.hashtext(p_symbol));
  select max(closed_at) into last_at from public.lens_candles_5m where symbol = p_symbol;
  with incoming as (
    insert into public.lens_candles_5m as saved (symbol, closed_at, previous_at, previous_verified, high, low)
    select p_symbol, x.closed_at, x.previous_at, x.previous_verified, x.high, x.low
    from pg_catalog.jsonb_to_recordset(p_bars) as x(
      closed_at timestamptz, previous_at timestamptz, previous_verified boolean, high numeric, low numeric)
    on conflict (symbol, closed_at) do update
      set previous_at = excluded.previous_at, previous_verified = true
      where excluded.previous_verified and not saved.previous_verified
    returning closed_at
  )
  select max(closed_at) into latest_at from incoming;
  if latest_at::date > last_at::date then
    delete from public.lens_candles_5m
    where symbol = p_symbol and closed_at < latest_at - interval '30 days';
  end if;
  perform public.reconcile_lens_trades(p_symbol);
end;
$$;

revoke all on function public.reconcile_lens_trades(text) from public, anon, authenticated;
revoke all on function public.record_lens_candles(text, jsonb) from public, anon, authenticated;
grant execute on function public.reconcile_lens_trades(text) to service_role;
grant execute on function public.record_lens_candles(text, jsonb) to service_role;

create view public.lens_trade_stats with (security_invoker = true) as
with samples as (
  select symbol,
    case event_type when 'LONG_SETUP' then 'CALL' else 'PUT' end as side,
    candle_time at time zone 'America/New_York' as ny_time, outcome_status
  from public.signals_log
  where status='sent' and timeframe='5' and outcome_status is not null
    and event_type in ('LONG_SETUP', 'SHORT_SETUP')
), totals as (
  select interval_type.period, date_trunc(interval_type.unit, samples.ny_time)::date as period_start,
    symbol, side,
    count(*) filter (where outcome_status='win') as wins,
    count(*) filter (where outcome_status='loss') as losses,
    count(*) filter (where outcome_status='open') as open_trades
  from samples cross join (values ('daily', 'day'), ('weekly', 'week')) interval_type(period, unit)
  group by 1, 2, 3, 4
)
select *, round(100.0 * wins / nullif(wins + losses, 0), 2) as win_rate_pct from totals;

revoke all on public.lens_trade_stats from public, anon, authenticated;
grant select on public.lens_trade_stats to service_role;
